"""The public owner of fixed instructions and ordered plugin guidance."""

from harnessed.core.plugin import Plugin
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.prompt.values import InstructionValues, PromptPlan
from harnessed.kernel import Context


class Instructions(Plugin, slot="instructions"):
    """Render literal instructions and file sections once when a session opens."""

    needs = (SystemPrompt,)

    def __init__(self, values: InstructionValues = (), *, plan: PromptPlan | None = None):
        super().__init__()
        self.values = (values,) if isinstance(values, str) else tuple(values)
        self.plan = plan

    async def setup(self, ctx: Context):
        plan = self.plan if self.plan is not None else PromptPlan(self.values)
        ctx[SystemPrompt].fixed(await plan.render(), owner=ctx)
