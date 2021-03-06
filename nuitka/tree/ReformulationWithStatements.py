#     Copyright 2020, Kay Hayen, mailto:kay.hayen@gmail.com
#
#     Part of "Nuitka", an optimizing Python compiler that is compatible and
#     integrates with CPython, but also works on its own.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
#
""" Reformulation of with statements.

Consult the developer manual for information. TODO: Add ability to sync
source code comments with developer manual sections.

"""

from nuitka import Options
from nuitka.nodes.AssignNodes import (
    StatementAssignmentVariable,
    StatementReleaseVariable,
)
from nuitka.nodes.AttributeNodes import (
    ExpressionAttributeLookup,
    ExpressionAttributeLookupSpecial,
)
from nuitka.nodes.CallNodes import (
    ExpressionCallEmpty,
    ExpressionCallNoKeywords,
)
from nuitka.nodes.ComparisonNodes import ExpressionComparisonIs
from nuitka.nodes.ConditionalNodes import makeStatementConditional
from nuitka.nodes.ConstantRefNodes import makeConstantRefNode
from nuitka.nodes.ContainerMakingNodes import makeExpressionMakeTuple
from nuitka.nodes.CoroutineNodes import (
    ExpressionAsyncWaitEnter,
    ExpressionAsyncWaitExit,
)
from nuitka.nodes.ExceptionNodes import (
    ExpressionCaughtExceptionTracebackRef,
    ExpressionCaughtExceptionTypeRef,
    ExpressionCaughtExceptionValueRef,
)
from nuitka.nodes.StatementNodes import (
    StatementExpressionOnly,
    StatementsSequence,
)
from nuitka.nodes.VariableRefNodes import ExpressionTempVariableRef
from nuitka.nodes.YieldNodes import ExpressionYieldFromWaitable
from nuitka.PythonVersions import python_version

from .ReformulationAssignmentStatements import buildAssignmentStatements
from .ReformulationTryExceptStatements import (
    makeTryExceptSingleHandlerNodeWithPublish,
)
from .ReformulationTryFinallyStatements import makeTryFinallyStatement
from .TreeHelpers import (
    buildNode,
    buildStatementsNode,
    makeReraiseExceptionStatement,
    makeStatementsSequence,
)


def _buildWithNode(provider, context_expr, assign_target, body, sync, source_ref):
    # Many details, pylint: disable=too-many-locals
    with_source = buildNode(provider, context_expr, source_ref)

    if python_version < 0x380 and Options.is_fullcompat:
        source_ref = with_source.getCompatibleSourceReference()

    temp_scope = provider.allocateTempScope("with")

    tmp_source_variable = provider.allocateTempVariable(
        temp_scope=temp_scope, name="source"
    )
    tmp_exit_variable = provider.allocateTempVariable(
        temp_scope=temp_scope, name="exit"
    )
    tmp_enter_variable = provider.allocateTempVariable(
        temp_scope=temp_scope, name="enter"
    )

    # Indicator variable, will end up with C bool type, and need not be released.
    tmp_indicator_variable = provider.allocateTempVariable(
        temp_scope=temp_scope, name="indicator", temp_type="bool"
    )

    statements = (
        buildAssignmentStatements(
            provider=provider,
            node=assign_target,
            allow_none=True,
            source=ExpressionTempVariableRef(
                variable=tmp_enter_variable, source_ref=source_ref
            ),
            source_ref=source_ref,
        ),
        body,
    )

    with_body = makeStatementsSequence(
        statements=statements, allow_none=True, source_ref=source_ref
    )

    if body:
        deepest = body

        while deepest.getVisitableNodes():
            deepest = deepest.getVisitableNodes()[-1]

        if python_version < 0x370:
            body_lineno = deepest.getCompatibleSourceReference().getLineNumber()
        else:
            body_lineno = deepest.getSourceReference().getLineNumber()

        with_exit_source_ref = source_ref.atLineNumber(body_lineno)
    else:
        with_exit_source_ref = source_ref

    # The "__enter__" and "__exit__" were normal attribute lookups under
    # CPython2.6, but that changed with CPython2.7.
    if python_version < 0x270:
        attribute_lookup_class = ExpressionAttributeLookup
    else:
        attribute_lookup_class = ExpressionAttributeLookupSpecial

    enter_value = ExpressionCallEmpty(
        called=attribute_lookup_class(
            expression=ExpressionTempVariableRef(
                variable=tmp_source_variable, source_ref=source_ref
            ),
            attribute_name="__enter__" if sync else "__aenter__",
            source_ref=source_ref,
        ),
        source_ref=source_ref,
    )

    exit_value_exception = ExpressionCallNoKeywords(
        called=ExpressionTempVariableRef(
            variable=tmp_exit_variable, source_ref=with_exit_source_ref
        ),
        args=makeExpressionMakeTuple(
            elements=(
                ExpressionCaughtExceptionTypeRef(source_ref=with_exit_source_ref),
                ExpressionCaughtExceptionValueRef(source_ref=with_exit_source_ref),
                ExpressionCaughtExceptionTracebackRef(source_ref=source_ref),
            ),
            source_ref=source_ref,
        ),
        source_ref=with_exit_source_ref,
    )

    exit_value_no_exception = ExpressionCallNoKeywords(
        called=ExpressionTempVariableRef(
            variable=tmp_exit_variable, source_ref=source_ref
        ),
        args=makeConstantRefNode(constant=(None, None, None), source_ref=source_ref),
        source_ref=with_exit_source_ref,
    )

    # For "async with", await the entered value and exit value must be awaited.
    if not sync:
        enter_value = ExpressionYieldFromWaitable(
            expression=ExpressionAsyncWaitEnter(
                expression=enter_value, source_ref=source_ref
            ),
            source_ref=source_ref,
        )
        exit_value_exception = ExpressionYieldFromWaitable(
            expression=ExpressionAsyncWaitExit(
                expression=exit_value_exception, source_ref=source_ref
            ),
            source_ref=source_ref,
        )
        exit_value_no_exception = ExpressionYieldFromWaitable(
            ExpressionAsyncWaitExit(
                expression=exit_value_no_exception, source_ref=source_ref
            ),
            source_ref=source_ref,
        )

    statements = [
        # First assign the with context to a temporary variable.
        StatementAssignmentVariable(
            variable=tmp_source_variable, source=with_source, source_ref=source_ref
        )
    ]

    attribute_assignments = [
        # Next, assign "__enter__" and "__exit__" attributes to temporary
        # variables.
        StatementAssignmentVariable(
            variable=tmp_exit_variable,
            source=attribute_lookup_class(
                expression=ExpressionTempVariableRef(
                    variable=tmp_source_variable, source_ref=source_ref
                ),
                attribute_name="__exit__" if sync else "__aexit__",
                source_ref=source_ref,
            ),
            source_ref=source_ref,
        ),
        StatementAssignmentVariable(
            variable=tmp_enter_variable, source=enter_value, source_ref=source_ref
        ),
    ]

    if python_version >= 0x360 and sync:
        attribute_assignments.reverse()

    statements += attribute_assignments

    statements.append(
        StatementAssignmentVariable(
            variable=tmp_indicator_variable,
            source=makeConstantRefNode(constant=True, source_ref=source_ref),
            source_ref=source_ref,
        )
    )

    statements += (
        makeTryFinallyStatement(
            provider=provider,
            tried=makeTryExceptSingleHandlerNodeWithPublish(
                provider=provider,
                tried=with_body,
                exception_name="BaseException",
                handler_body=StatementsSequence(
                    statements=(
                        # Prevents final block from calling __exit__ as
                        # well.
                        StatementAssignmentVariable(
                            variable=tmp_indicator_variable,
                            source=makeConstantRefNode(
                                constant=False, source_ref=source_ref
                            ),
                            source_ref=source_ref,
                        ),
                        makeStatementConditional(
                            condition=exit_value_exception,
                            no_branch=makeReraiseExceptionStatement(
                                source_ref=with_exit_source_ref
                            ),
                            yes_branch=None,
                            source_ref=with_exit_source_ref,
                        ),
                    ),
                    source_ref=source_ref,
                ),
                public_exc=python_version >= 0x270,
                source_ref=source_ref,
            ),
            final=makeStatementConditional(
                condition=ExpressionComparisonIs(
                    left=ExpressionTempVariableRef(
                        variable=tmp_indicator_variable, source_ref=source_ref
                    ),
                    right=makeConstantRefNode(constant=True, source_ref=source_ref),
                    source_ref=source_ref,
                ),
                yes_branch=StatementExpressionOnly(
                    expression=exit_value_no_exception, source_ref=source_ref
                ),
                no_branch=None,
                source_ref=source_ref,
            ),
            source_ref=source_ref,
        ),
    )

    return makeTryFinallyStatement(
        provider=provider,
        tried=statements,
        final=(
            StatementReleaseVariable(
                variable=tmp_source_variable, source_ref=with_exit_source_ref
            ),
            StatementReleaseVariable(
                variable=tmp_enter_variable, source_ref=with_exit_source_ref
            ),
            StatementReleaseVariable(
                variable=tmp_exit_variable, source_ref=with_exit_source_ref
            ),
        ),
        source_ref=source_ref,
    )


def buildWithNode(provider, node, source_ref):
    # "with" statements are re-formulated as described in the developer
    # manual. Catches exceptions, and provides them to "__exit__", while making
    # the "__enter__" value available under a given name.

    # Before Python3.3, multiple context managers are not visible in the parse
    # tree, now we need to handle it ourselves.
    if hasattr(node, "items"):
        context_exprs = [item.context_expr for item in node.items]
        assign_targets = [item.optional_vars for item in node.items]
    else:
        # Make it a list for before Python3.3
        context_exprs = [node.context_expr]
        assign_targets = [node.optional_vars]

    # The body for the first context manager is the other things.
    body = buildStatementsNode(provider, node.body, source_ref)

    assert context_exprs and len(context_exprs) == len(assign_targets)

    context_exprs.reverse()
    assign_targets.reverse()

    for context_expr, assign_target in zip(context_exprs, assign_targets):
        body = _buildWithNode(
            provider=provider,
            body=body,
            context_expr=context_expr,
            assign_target=assign_target,
            sync=True,
            source_ref=source_ref,
        )

    return body


def buildAsyncWithNode(provider, node, source_ref):
    # "with" statements are re-formulated as described in the developer
    # manual. Catches exceptions, and provides them to "__exit__", while making
    # the "__enter__" value available under a given name.

    # Before Python3.3, multiple context managers are not visible in the parse
    # tree, now we need to handle it ourselves.
    context_exprs = [item.context_expr for item in node.items]
    assign_targets = [item.optional_vars for item in node.items]

    # The body for the first context manager is the other things.
    body = buildStatementsNode(provider, node.body, source_ref)

    assert context_exprs and len(context_exprs) == len(assign_targets)

    context_exprs.reverse()
    assign_targets.reverse()

    for context_expr, assign_target in zip(context_exprs, assign_targets):
        body = _buildWithNode(
            provider=provider,
            body=body,
            context_expr=context_expr,
            assign_target=assign_target,
            sync=False,
            source_ref=source_ref,
        )

    return body
