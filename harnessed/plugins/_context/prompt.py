"""The sole system-prompt owner used by the application facade."""

from harnessed.core.instructions import Instructions, PromptPlan
from harnessed.core.prompt import SystemPrompt
from harnessed.kernel import Context, plugin


@plugin(needs=[SystemPrompt], slot="instructions")
async def instructions(ctx: Context, values: Instructions = (), *, plan: PromptPlan | None = None):
    plan = plan if plan is not None else PromptPlan(values)
    ctx[SystemPrompt].fixed(await plan.render(), owner=ctx)
    yield
