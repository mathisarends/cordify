import pytest
from pydantic import ValidationError

from harnessed import Agent, ApprovalAnswer, Plugin, PolicyDecision, tool
from harnessed.core.approvals import CallbackApprover
from harnessed.core.session.events import ApprovalRequested
from harnessed.plugins import Approvals, Filesystem
from harnessed.testing import ScriptedModel, call


class Echo(Plugin):
    @tool(name="echo_text", readonly=True)
    async def echo(self, text: str) -> str:
        return text


async def test_required_reference_overrides_default_trust_and_policy():
    requests = []

    def approve(request):
        requests.append(request)
        return ApprovalAnswer.ALLOW

    model = ScriptedModel([call(Echo.echo, text="hello"), "done"])
    agent = Agent(
        model=model,
        plugins=[
            Echo(),
            Approvals(
                default=PolicyDecision.ALLOW,
                trust=[Echo.echo],
                require=[Echo.echo],
                policy=lambda call: PolicyDecision.ALLOW,
                approver=approve,
            ),
        ],
    )
    result = await agent.run("Echo")
    assert result.tool_calls[0][1].text == "hello"
    assert len(requests) == 1
    assert isinstance(requests[0], ApprovalRequested)
    assert requests[0].call.name == "echo_text"


async def test_denied_reference_never_asks():
    model = ScriptedModel([call(Echo.echo, text="hello"), "done"])
    agent = Agent(
        model=model,
        plugins=[Echo(), Approvals(require=[Echo.echo], deny=[Echo.echo])],
    )
    result = await agent.run("Echo")
    assert result.tool_calls[0][1].is_error


async def test_unmounted_reference_and_wrong_recipe_fail_at_session_start():
    for plugins in ([Approvals(require=[Echo.echo])], [Echo(), Approvals(require=[Echo().echo])]):
        agent = Agent(model=ScriptedModel([]), plugins=plugins)
        with pytest.raises(ValueError, match="not registered"):
            async with agent.session():
                pytest.fail("unresolved references must fail before exposing the session")


async def test_bound_recipe_reference_survives_per_session_mount_copy():
    echo = Echo()
    model = ScriptedModel([call(echo.echo, text="hello"), "done"])
    agent = Agent(
        model=model,
        plugins=[
            echo,
            Approvals(require=[echo.echo], approver=lambda request: ApprovalAnswer.ALLOW),
        ],
    )
    assert (await agent.run("Echo")).tool_calls[0][1].text == "hello"


def test_scripted_references_validate_arguments_without_self_or_injected_services():
    response = call(Filesystem.read_file, path="large.log", offset=40, limit=40)
    assert response.message.tool_calls[0].name == "read_file"
    with pytest.raises(ValidationError):
        call(Filesystem.read_file, path="large.log", limit="40")
    with pytest.raises(ValidationError):
        call(Filesystem.read_file, path="large.log", limit=0)
    with pytest.raises(ValidationError):
        call(Filesystem.read_file, path="large.log", fs="injected")


async def test_callback_adapter_receives_the_same_request_as_policy_callbacks():
    request = ApprovalRequested(
        "request", call(Echo.echo, text="hi").message.tool_calls[0], "why", "test"
    )
    seen = []
    adapter = CallbackApprover(lambda value: seen.append(value) or True)
    assert await adapter.ask(request) is ApprovalAnswer.ALLOW
    assert seen == [request]


def test_string_sentinel_and_arbitrary_functions_are_rejected():
    with pytest.raises(TypeError, match="collections"):
        Approvals(trust="all")
    with pytest.raises(TypeError, match="@tool"):
        Approvals(require=[lambda: None])
