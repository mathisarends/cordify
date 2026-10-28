from harnessed.core.plugin import Plugin
from harnessed.core.prompt.system_prompt import SystemPrompt
from harnessed.core.prompt.values import InstructionValues, PromptComposition
from harnessed.kernel import Context


class Instructions(Plugin, slot="instructions", needs=(SystemPrompt,)):
    """Render literal instructions and file sections once when a session opens."""

    def __init__(
        self, values: InstructionValues = (), *, composition: PromptComposition | None = None
    ) -> None:
        super().__init__()
        self.values = (values,) if isinstance(values, str) else tuple(values)
        self.composition = composition

    async def setup(self, ctx: Context) -> None:
        composition = self.composition or PromptComposition(self.values)
        ctx[SystemPrompt].fixed(await composition.render(ctx), owner=ctx)
