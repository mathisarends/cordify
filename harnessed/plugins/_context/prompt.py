"""The sole system-prompt owner used by the application facade."""

from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.prompt.values import InstructionValues, PromptPlan
from harnessed.kernel import Context, plugin


@plugin(needs=[SystemPrompt], slot="instructions")
async def instructions(
    ctx: Context, values: InstructionValues = (), *, plan: PromptPlan | None = None
):
    plan = plan if plan is not None else PromptPlan(values)
    ctx[SystemPrompt].fixed(await plan.render(), owner=ctx)
    yield
