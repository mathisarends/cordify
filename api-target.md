# Harnessed – Ziel-API auf einen Blick

> **Status:** Vom Nutzer bestätigtes Zielbild. Der Umsetzungsstand steht in `review.md`. Alle Namen und Signaturen folgen den Empfehlungen in [`review.md`](review.md) (IDs wie P0-5 verweisen dorthin).
> **Zweck:** Die Developer Experience beurteilen, bevor gebaut wird. Wenn ein Abschnitt hier zu viel Code für das braucht, was er tut, ist das ein DX-Problem.
> **Aktueller Umfang:** Subagents sind auf Nutzerwunsch zurückgestellt. Abschnitt 11 bleibt eine spätere Designskizze; `Subagent`, Kind-Session-Logs, `session.children` und geerbte Parent-Approvals werden in dieser Iteration nicht implementiert.

## Prinzip: Everything is a plugin

Harnessed bleibt beim Prinzip von Cordis und DSH: Ein Agent ist eine Liste von Plugins. Auch der Loop, das Model, die Tools, die Persistenz und die Approval-Policy sind Plugins. `Agent(...)` ist nur **Zucker** darüber und kein zweites System.

**Regeln:**
1. **`Agent` hat genau drei Parameter:** `model`, `instructions` und `plugins`. Alles andere ist ein Plugin in `plugins=[...]`: Tools, Persistenz, Approvals, Compaction, Spill, Skills und Services; später auch Subagents.
2. **Tools leben immer in Plugins**, als `@tool`-Methoden. Lose Funktionen als Tools gibt es nicht, und es gibt genau einen Registrierungsweg.
3. **Jeder Parameter entspricht 1:1 einem öffentlichen Plugin.** Ein Test sichert das ab:
   ```python
   Agent(model=x, instructions=s, plugins=[Calculator()])
   # is exactly
   Agent(plugins=[Service(Model, x), Instructions(s), Calculator()])
   ```
4. **Defaults sind normale Plugins mit einem Slot.** Ein Plugin mit demselben Slot ersetzt den Default. Zwei Plugins mit demselben Slot in `plugins=` führen zu einem `PluginConflict`.
5. **Built-ins haben keinen privilegierten Weg.** Sie nutzen nur öffentliche API (P1-6), also kann jeder sie nachbauen oder ersetzen.
6. **`agent.plugins` zeigt die vollständige, aufgelöste Zusammensetzung.**

```python
>>> agent.plugins
(AgentLoop(), MemorySessions(), ToolOutputLimit(max_chars=32000), Approvals(), NoopTracer(),
 Service(Model, PydanticAIModel('deepseek:deepseek-chat')), Instructions('Keep answers concise.'), Calculator())
```

| Slot | Default | Ersetzen durch |
|---|---|---|
| `loop` | `AgentLoop()` | `AgentLoop(max_steps=..., tool_timeout=..., ...)` oder ein eigener Loop |
| `sessions` | `MemorySessions()` | `JsonlSessions(path)` |
| `output_limit` | `ToolOutputLimit(max_chars=32_000)` | `ToolOutputLimit(...)` |
| `approvals` | `Approvals()`: readonly → ALLOW, sonst ASK, kein Approver | `Approvals(approver=..., trust=...)` |
| `tracer` | `NoopTracer()` | eigener Tracer (OTel-Exporter später, Abschnitt 15) |

**Benennung:** Alle mitgelieferten Plugins sind Klassen und heißen deshalb in CapWords (`AgentLoop`, `JsonlSessions`, `Subagent`, `Approvals`). Ob ein Plugin intern als Klasse oder als Generator gebaut ist, ändert seinen Namen nie. Die Funktionsform mit `@plugin` (klein geschrieben) bleibt für eigene, kleine Plugins von Nutzern. Kleinbuchstaben in `plugins=[...]` bedeuten daher: selbst geschrieben. Hilfsfunktionen, die keine Plugins sind (`at_ratio`, `every_turns`), bleiben klein.

## Die drei Ebenen

| Ebene | Für wen | Importiert aus | Muss man kennen |
|---|---|---|---|
| **1 · App** | Wer einen Agent baut | `harnessed`, `harnessed.models`, `harnessed.plugins`, `harnessed.errors` | `Agent`, `Session`, `RunResult`, Stream-Events, mitgelieferte Plugins *benutzen*, Tool-Plugins (`Plugin` + `@tool`) *schreiben* |
| **2 · Plugin** | Wer das Verhalten des Loops ändert | zusätzlich `harnessed.hooks`, `harnessed.events` | `hook`, `Inject`, Hook-Events, Slots |
| **3 · Kernel** | Wer ein eigenes Framework darauf baut | `harnessed.kernel`, bei Bedarf die besitzende `harnessed.core`-Schicht | `Context`, Services, `needs`, Effects |

Ein Plugin, das nur Tools enthält, ist die einfachste Plugin-Form. Es braucht weder Context noch Hooks und gehört deshalb schon zu Ebene 1. Hooks beginnen in Abschnitt 14.

## Spickzettel

```python
from harnessed import (
    Agent, Session, SessionStatus, RunHandle, RunResult, StopReason,
    Plugin, PluginConfig, tool, Section, ToolError, ToolContext, Image,
    ApprovalRequest, PolicyDecision, events,
)
from harnessed.kernel import Context, hook, Inject
from harnessed.hooks import Next, PrepareRequest, ToolPolicy, ReviewToolResult, Priority
from harnessed.models import PydanticAIModel
from harnessed.plugins import (
    AgentLoop, JsonlSessions, Service, ContextValue,
    Approvals, ApprovalAnswer, Compaction, at_ratio, every_turns, Spill, ToolOutputLimit,
    Filesystem, Shell, Skills, Skill,
)
from harnessed.testing import ScriptedModel, call
```

---

## 1. Hello World

```python
import asyncio
from harnessed import Agent, Plugin, tool


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


agent = Agent(
    model="deepseek:deepseek-chat",  # API key from DEEPSEEK_API_KEY, extra: harnessed[openai]
    instructions="Keep answers concise.",
    plugins=[Calculator()],
)


async def main():
    async with agent.session() as session:  # in-memory, random id
        result = await session.run("What is 19 + 23?")
        print(result.text)


asyncio.run(main())
```

**Begriffe:**
- **`Agent`** ist eine wiederverwendbare, zustandslose *Konfiguration*, also eine Plugin-Liste. Man kann sie als Modul-Global halten.
- **`Session`** ist ein laufendes Gespräch mit Log. Es entsteht über `agent.session(...)` und ist ein async context manager.
- **`model`** ist ein `Model` oder ein pydantic-ai-String wie `"deepseek:deepseek-chat"` (Abschnitt 3).
- **`instructions`** ist der komplette System-Prompt, nicht der User-Prompt. Der User-Prompt ist das Argument von `session.run(...)` (Abschnitt 4).

**Ein typischer Produktions-Agent:**

```python
agent = Agent(
    model="deepseek:deepseek-chat",
    instructions="You are a careful support agent.",
    plugins=[
        Orders(api_url="https://orders.internal"),  # lookup_order, refund
        JsonlSessions(".harnessed/sessions"),
        Approvals(approver=ask_in_slack, trust=["lookup_order"]),
        Compaction(trigger=at_ratio(0.8)),
    ],
)
```

---

## 2. Tools

Tools sind `@tool`-Methoden eines Plugins. Das Plugin ist die natürliche Einheit für zusammengehörige Tools: Es hält ihre Config, ihren Zustand (z. B. einen HTTP-Client) und ihren Lebenszyklus.

### 2.1 Grundform

```python
class Weather(Plugin):
    @tool(readonly=True)
    async def get_weather(self, city: str, unit: Literal["c", "f"] = "c") -> str:
        """Current weather for a city."""
        ...

    @tool(readonly=True)
    async def get_forecast(self, city: str, days: int = 3) -> str:
        """Forecast for the next days."""
        ...


agent = Agent(..., plugins=[Weather()])
```

- Der Tool-Name ist der Methodenname, die Beschreibung ist der Docstring. Das JSON-Schema kommt aus den Type Hints (Pydantic, strict). `self` ist für das Modell unsichtbar.
- Sync-Methoden sind erlaubt und laufen in einem Thread. Sie sind **nicht abbrechbar** (P1-9).
- Der Rückgabewert darf `str`, `Image`, eine Liste daraus, ein Pydantic-Modell oder ein `dict` sein. Modelle und Dicts werden als JSON serialisiert.

Öffentliche Tools werden ausschließlich als `@tool`-Methoden eines Plugins registriert.
`ctx.tool`, `Bundle.tool`, `Session.tool`, `add_tool` und Funktionslisten sind keine
weiteren öffentlichen Wege. Kleine funktionale Kernel-Plugins bleiben für Services
und Hooks verfügbar; für Tools wird auch bei wenigen Methoden eine Klasse verwendet.

### 2.2 Config, Zustand und Lebenszyklus

```python
class GitHubConfig(PluginConfig):
    token: SecretStr
    default_org: str | None = None


class GitHub(Plugin):
    Config = GitHubConfig
    config: GitHubConfig

    def __init__(self, token: SecretStr, default_org: str | None = None):
        super().__init__(token=token, default_org=default_org)

    async def setup(self, ctx):  # per mount; state lives here only
        self.client = GitHubClient(self.config.token)
        ctx.effect(self.client.aclose)  # closed on dispose

    @tool(readonly=True)
    async def list_issues(self, repo: str) -> str:
        """List open issues of a repository."""
        return await self.client.issues(repo)

    @tool
    async def create_issue(self, repo: str, title: str, body: str) -> str:
        """Create an issue."""
        ...


agent = Agent(..., plugins=[GitHub(token=os.environ["GITHUB_TOKEN"])])  # validated by Config
```

### 2.3 Parameter mit Grenzen

```python
from typing import Annotated
from pydantic import BaseModel, Field


class Range(BaseModel):
    start: Annotated[int, Field(ge=1)]
    end: Annotated[int, Field(ge=1)]


class Files(Plugin):
    @tool(readonly=True)
    async def read_lines(
        self,
        path: Annotated[str, Field(description="Path relative to the workspace")],
        lines: Range | None = None,
        limit: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> str:
        """Read lines from a file."""
```

Die Grenzen stehen im JSON-Schema, das Modell sieht sie also. Ungültige Argumente gehen als Validierungsfehler zurück ans Modell, nicht als Exception an den Entwickler.

### 2.4 Wirkung deklarieren (steuert Approvals)

```python
@tool(readonly=True)  # never asks
async def search(self, query: str) -> str: ...


@tool  # default: side_effects=True -> ASK unless trusted
async def send_email(self, to: str, body: str) -> str: ...
```

Was das für Approvals bedeutet, steht in Abschnitt 9.

### 2.5 Optionen am Decorator

```python
@tool(
    name="fs.write",  # default: method name
    description="...",  # default: docstring
    readonly=False,
    timeout=30,  # seconds, default from AgentLoop(tool_timeout=...)
    resources=lambda args: [f"file:{args['path']}"],  # parallel scheduling: same key = serialized
    output_limit=8_000,  # overrides ToolOutputLimit for this tool
    override=True,  # explicitly replace an existing tool with this name
)
async def write_file(self, path: str, content: str) -> str: ...
```

Ohne `resources` laufen mehrere Tool-Calls eines Steps parallel. Gleiche Ressourcen-Keys werden serialisiert. Die Ergebnisse landen immer in Aufrufreihenfolge im Log.

### 2.6 Laufzeit-Kontext und Services im Tool

```python
class Deployer(Plugin):
    @tool
    async def deploy(
        self,
        service: str,
        *,
        ctx: ToolContext,  # per call, invisible to the model
        http: Inject[httpx.AsyncClient],  # any service from the Agent's context
    ) -> str:
        """Deploy a service."""
        await ctx.progress(f"deploying {service}")  # shows up as ToolProgress in the stream
        async for line in stream_logs(http, service):
            if ctx.cancelled:  # cooperative cancellation
                raise ToolError("Deployment cancelled.")
        return "ok"
```

- Keyword-only Parameter mit `ToolContext` oder `Inject[...]` sind für das Modell unsichtbar.
- `ToolContext` enthält `call`, `session`, `progress()` und `cancelled`, aber **nicht** den Agent (P1-9).
- **Faustregel:** Was nur dieses Plugin braucht, entsteht in `setup()` und liegt auf `self` (2.2). Was mehrere Plugins teilen, ist ein Service und kommt über `Inject`.

Ein Service ist ebenfalls ein Plugin. Für eine fertige Instanz reicht `Service`:

```python
agent = Agent(..., plugins=[Service(httpx.AsyncClient, httpx.AsyncClient()), Deployer()])
```

### 2.7 Fehler

```python
class Users(Plugin):
    @tool(readonly=True)
    async def get_user(self, user_id: str, *, db: Inject[Database]) -> str:
        """Look up a user."""
        user = await db.find(user_id)
        if user is None:
            raise ToolError(f"No user with id {user_id}.")  # this text goes to the model
        return user.json()
```

- **`ToolError(msg)`:** `msg` geht so ans Modell, wie es ist.
- **Jede andere Exception:** Das Modell bekommt nur `"get_user failed: KeyError"`. Die volle Exception landet im Logging (P0-5).
- Anpassen lässt sich das über das Loop-Plugin: `AgentLoop(error_formatter=...)`.

### 2.8 Multimodale Ergebnisse

```python
class Browser(Plugin):
    @tool(readonly=True)
    async def screenshot(self, url: str) -> list[str | Image]:
        """Take a screenshot of a page."""
        png = await self.page.screenshot(url)
        return ["Screenshot of " + url, Image(png, media_type="image/png")]
```

### 2.9 Tools zur Laufzeit

Auch zur Laufzeit gibt es keinen Sonderweg: Man aktiviert ein Plugin in der Session.

```python
handle = await session.use(GitHub(token=...))  # tools are available from the next step on
await handle.aclose()  # removes plugin and tools again
```

- Gibt es einen Tool-Namen doppelt, scheitert der Start mit **`ToolNameConflict`**, es sei denn, das spätere Tool setzt `override=True` (P1-9).
- Spätere Subagents sollen ebenfalls Plugins sein, die Tools beisteuern (zurückgestellt, Abschnitt 11).

---

## 3. Models

> **Entscheidung:** Das Protokoll heißt `Model`, der Parameter `model=`. Provider-Anbindungen bauen wir **nicht selbst**, sondern nutzen [pydantic-ai](https://ai.pydantic.dev) als optionale Integration. Es gibt genau einen Adapter, `PydanticAIModel`, und **keine eigenen Provider-Klassen** wie `AnthropicModel` oder `DeepSeekModel`. Begründung in 3.5.

### 3.1 Drei Formen für `model=`

```python
# 1. normal case: a pydantic-ai model string, no extra import
agent = Agent(model="deepseek:deepseek-chat")  # key: DEEPSEEK_API_KEY, extra: harnessed[openai]
agent = Agent(
    model="anthropic:claude-sonnet-4-5"
)  # key: ANTHROPIC_API_KEY, extra: harnessed[anthropic]

# 2. special configuration (Azure, own base_url, vLLM, Ollama, ...): pass the pydantic-ai model directly
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.azure import AzureProvider

agent = Agent(model=OpenAIChatModel("gpt-5", provider=AzureProvider(...)))

# 3. our own options: wrap explicitly
from harnessed.models import PydanticAIModel

agent = Agent(model=PydanticAIModel("deepseek:deepseek-chat", context_window=128_000))
```

- **`model=` nimmt `Model | pydantic_ai.models.Model | str`.** Strings und pydantic-ai-Models werden in `PydanticAIModel(...)` verpackt. Das ist keine versteckte Magie: `agent.plugins` zeigt das Ergebnis, etwa `Service(Model, PydanticAIModel('deepseek:deepseek-chat'))`.
- **Strings folgen der pydantic-ai-Konvention** `"<provider>:<model>"` und werden über `pydantic_ai.models.infer_model` aufgelöst. Wir erfinden kein eigenes Format.
- **`model=x` ist Zucker für `plugins=[Service(Model, x)]`.** Eine eigene Plugin-Klasse für das Model gibt es nicht. Wer das Model zur Laufzeit tauschen will, ersetzt diesen Service. Abhängige Plugins starten dann automatisch neu (temporale `needs`).
- **Den HTTP-Client verwaltet pydantic-ai** über seinen Provider. `PydanticAIModel` schließt nur Clients, die es selbst erzeugt hat.

### 3.2 Installation

pydantic-ai ist **keine** Kernabhängigkeit. Der Kern braucht nur `pydantic`. Die Provider kommen über Extras, die auf `pydantic-ai-slim` zeigen:

```toml
[project.optional-dependencies]
openai = ["pydantic-ai-slim[openai]"]         # OpenAI, Azure, DeepSeek, OpenRouter, vLLM, Ollama, ...
anthropic = ["pydantic-ai-slim[anthropic]"]
google = ["pydantic-ai-slim[google]"]
```

- `pip install harnessed[openai]` reicht für DeepSeek, weil DeepSeek die OpenAI-kompatible API nutzt.
- Fehlt das Extra, wirft `Agent(model="anthropic:...")` einen `ConfigError` mit dem genauen Befehl, z. B. `pip install harnessed[anthropic]`.
- `pydantic_ai` wird an **genau einer Stelle** importiert: `harnessed/models/pydantic_ai.py` (Schicht `models`). Kernel und Core kennen das Provider-SDK nicht.

### 3.3 Einstellungen

Model-Settings sind die `ModelSettings` von pydantic-ai, wir definieren keine eigenen. Loop-Verhalten gehört zum Loop-Plugin:

```python
from pydantic_ai.settings import ModelSettings
from harnessed.models import TokenLimitBehavior

agent = Agent(
    model=PydanticAIModel(
        "deepseek:deepseek-reasoner",
        settings=ModelSettings(temperature=0.2, max_tokens=8_000, parallel_tool_calls=True),
    ),
    plugins=[
        AgentLoop(
            max_steps=50, tool_timeout=30, on_max_tokens=TokenLimitBehavior.CONTINUE
        ),  # replaces the default loop
    ],
)

# per call, merged over the model's settings
await session.run("...", settings=ModelSettings(temperature=0))
```

- Provider-spezifische Optionen (Reasoning-Effort, Thinking, Prompt-Caching) sind die provider-spezifischen Keys der pydantic-ai-`ModelSettings`. Wir reichen sie unverändert durch.
- **Retries** macht pydantic-ai über den HTTP-Transport des Providers (`pydantic_ai.retries`). Eine eigene `RetryPolicy` gibt es nicht.
- **Fehler:** `PydanticAIModel` bildet die Fehler von pydantic-ai auf unsere typisierten Fehler ab (`AuthError`, `RateLimited`, `ContextLengthExceeded`, …, Abschnitt 16). Plugins wie `Compaction` reagieren nur auf unsere Typen.
- **Kontextfenster:** pydantic-ai kennt es nicht zuverlässig. `PydanticAIModel` bringt deshalb eine kleine Tabelle für bekannte Modelle mit und nimmt `context_window=` für alle anderen. Fehlt beides, warnt `Compaction(at_ratio(...))` beim Start.

### 3.4 Eigenes Model

`Model` ist unser eigenes Protokoll, öffentlich über `harnessed.models` verfügbar. Wer kein pydantic-ai will, implementiert es direkt:

```python
from harnessed.models import Model, ModelInfo, ModelRequest, ModelResponse, ModelStream


class MyModel:  # satisfies the Model protocol, no base class needed
    info = ModelInfo(
        name="my-model", context_window=200_000, max_output=16_000, supports_images=True
    )

    async def request(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> AsyncContextManager[ModelStream]: ...
```

- `request()` wird für einmalige Aufrufe genutzt (z. B. den Summarizer der Compaction), `stream()` vom Loop.
- `ModelStream` ist ein `AsyncIterator[ModelChunk]`. Nach dem Ende liefert `stream.response()` die fertige `ModelResponse`.
- Die Form spiegelt die Direct-API von pydantic-ai (`model_request`, `model_request_stream`). `PydanticAIModel` ist deshalb ein dünner Übersetzer zwischen den Message-Typen.
- Retries und Fehler-Mapping muss ein eigenes Model selbst mitbringen.

### 3.5 Warum keine eigenen Provider-Klassen

- **Kein Namenskonflikt.** `AnthropicModel`, `OpenAIChatModel` und `ModelSettings` gibt es bei pydantic-ai bereits. Gleichnamige eigene Klassen führen zu Verwechslungen beim Import und zu doppelter Pflege.
- **Kein Hinterherhinken.** Neue Provider, neue Modelle und Provider-Eigenheiten kommen mit pydantic-ai. DeepSeek etwa muss `reasoning_content` bei Tool-Runden zurückbekommen, sonst antwortet die API mit 400. pydantic-ai behandelt das schon.
- **Etablierte Konvention.** Die String-Form `"provider:model"` kennt, wer pydantic-ai kennt.
- **Ehrliche Abhängigkeit.** In Form 2 sieht man pydantic-ai-Typen in unserer API. Das ist gewollt, weil es zeigt, worauf wir aufbauen. Der Kern bleibt trotzdem unabhängig, weil `Model` unser Protokoll ist.
- **Bonus:** pydantic-ai kann Model-Requests selbst mit OpenTelemetry instrumentieren (Abschnitt 15).
- **Vor dem Umbau prüfen:** wie `model_request_stream` Reasoning- und Tool-Call-Deltas liefert, per Spike gegen DeepSeek und Anthropic.

---

## 4. Prompt: Instructions und Kontext

**Regel in einem Satz:** Der System-Prompt besteht aus den `instructions`, gefolgt von der `guidance` jedes Plugins in der Reihenfolge von `plugins=`.

Ein Plugin bringt seine Anleitung also selbst mit (Colocation). Wo sie landet, folgt aus einer festen Regel ohne Order-Zahlen, und `agent.prompt` zeigt das Ergebnis.

```python
from harnessed import Agent, Plugin, Section, tool


class GitHub(Plugin):
    guidance = "Call list_issues before create_issue."  # class attribute, appended automatically

    @tool(readonly=True)
    async def list_issues(self, repo: str) -> str:
        """List open issues."""


agent = Agent(
    model=...,
    instructions=[
        "You are a careful support agent.",
        Section.from_file("AGENTS.md"),  # read once at session start
    ],
    plugins=[GitHub(token=...), Weather()],  # Weather.guidance = "Prefer metric units."
)
```

### 4.1 Wie zusammengesetzt wird

Das Modell bekommt aus dem Beispiel oben genau das:

```
You are a careful support agent.

# Project rules
...

Call list_issues before create_issue.

Prefer metric units.
```

Dafür gelten vier Regeln ohne Ausnahme:

1. **Erst die `instructions`, in Listenreihenfolge.**
2. **Dann die `guidance` der Plugins, in der Reihenfolge von `plugins=`.** Plugins ohne `guidance` tragen nichts bei, und das gilt für alle Default-Plugins.
3. **Wörtlich.** Harnessed fügt keine Überschriften, Tags oder Präfixe hinzu. Wer Struktur will, schreibt sie in den Text.
4. **Getrennt durch eine Leerzeile.**

Weitere Eigenschaften:
- `instructions` ist ein `str` oder eine Liste aus `str` und `Section(name, text)`. Der Name dient nur dem Debugging und Fehlermeldungen.
- Der System-Prompt ist **pro Session fest**. Er wird beim Start der Session einmal gerendert, damit der Prompt-Cache des Providers hält.
- Ein Plugin, das erst zur Laufzeit per `session.use(...)` dazukommt, liefert seine `guidance` deshalb als Kontext (4.4).
- `instructions=` ist Zucker für das `Instructions([...])`-Plugin. Es ist das einzige Plugin, das den System-Prompt rendert. Andere Plugins liefern nur ihren `guidance`-Wert und rufen keine Prompt-API auf.

### 4.2 Die Regel überschreiben

Die Regel ist nur der Default. Drei Fälle lassen sich ohne Hook lösen:

```python
github = GitHub(token=...)

agent = Agent(
    model=...,
    instructions=[
        "You are a careful support agent.",
        github.guidance,  # 1. place it yourself; not appended again
        "Never close issues without asking.",
    ],
    plugins=[
        github,
        Weather(guidance=None),  # 2. drop it
        Shop(guidance="Only quote EUR prices."),  # 3. replace it
    ],
)
```

1. **Selbst platzieren:** Steht die `guidance` eines Plugins in `instructions`, erscheint sie dort und wird nicht noch einmal angehängt.
2. **Weglassen:** Jedes Plugin akzeptiert `guidance=None`.
3. **Ersetzen:** Jedes Plugin akzeptiert `guidance="..."`.

### 4.3 Sichtbar machen

```python
>>> print(agent.prompt)
System prompt (4 sections, ~410 tokens)
  1. "You are a careful support agent."  instructions[0]
  2. AGENTS.md "# Project rules …"       instructions[1], file read at session start
  3. "Call list_issues before…"          GitHub.guidance (plugins[0])
  4. "Prefer metric units."              Weather.guidance (plugins[1])
Context (dynamic, not in the system prompt)
  - date "Today is 2026-10-28."

>>> print(await session.prompt.render())   # exact text the model receives
```

Jede Zeile nennt ihre Herkunft. Damit ist sichtbar, was ein Plugin automatisch beiträgt.

### 4.4 Kontext: Fakten, die sich ändern

Was sich ändert (Datum, eingeloggter Nutzer, Rate-Limits), kommt **nicht** in den System-Prompt, sondern als Kontext. Auch dessen Form ist fest:

```python
from harnessed.plugins import ContextValue

agent = Agent(
    model=...,
    instructions="You are a careful support agent.",
    plugins=[
        ContextValue("date", lambda: f"Today is {date.today():%Y-%m-%d}.")
    ],  # evaluated per step
)

# at runtime, e.g. after login
session.context.set("user", f"The user is {user.name}.")
session.context.remove("user")
```

Das Modell bekommt Kontext als User-Message direkt vor der neuen Nachricht, und nur, wenn sich ein Wert geändert hat:

```
<context name="date">Today is 2026-10-28.</context>
```

- `session.context.set()` während eines laufenden Turns geht mit dem nächsten Step mit, startet aber nie selbst einen. Damit ersetzt es das frühere `inject` (8.1).
- Kontext ist durable im Log, ein Neustart verliert ihn also nicht.
- Compaction verwirft alte Snapshots und setzt den aktuellen neu.
- Liefert ein Callable `None`, entfällt der Eintrag.

### 4.5 Hinweise zu Tools

Was ein einzelnes Tool betrifft, gehört in seinen **Docstring**, den das Modell sowieso sieht. Eine `guidance` braucht ein Plugin nur für Regeln, die mehrere Tools betreffen.

> **Vergleich mit DSH:** Auch dort liefern Plugins ihre Sections selbst (`ctx.systemPrompt.section({name, order, text})`). Die Position bestimmt aber eine zentrale Tabelle mit Order-Zahlen (Identity −1000, Persona 0, `TOOL_BASH` 1000, …, Suffix 10200). Das passt zu einer geschlossenen App, in der ein Team alle Plugins kennt. In einem SDK mit Plugins von Dritten kollidieren solche Zahlen. Wir übernehmen deshalb die Colocation, ersetzen die Zahlen aber durch die Reihenfolge von `plugins=`. Die Trennung in System-Prompt und Kontext übernehmen wir direkt.

---

## 5. Sessions und Persistenz

```python
from harnessed.plugins import Durability, JsonlSessions

agent = Agent(
    model=...,
    plugins=[
        JsonlSessions(".harnessed/sessions", durability=Durability.FLUSH)
    ],  # Durability.BUFFERED, FLUSH oder FSYNC
)

async with agent.session("support-4711") as session:  # resumes if the log exists
    await session.run("...")

# reading
session.messages  # projected history (after compaction)
session.events  # full append-only log, typed
session.usage  # summed usage of the whole session
session.last(ToolFinished)

# fork: new session from a prefix of the log
async with agent.session("support-4711") as session:
    branch = session.fork("support-4711-alt", at=session.turns[-2].end)
async with agent.session(branch) as alt:
    await alt.run("Try a different approach.")

# managing
await agent.sessions.list()
await agent.sessions.delete("support-4711")  # also deletes spill files
```

- **Persistenz ist ein Plugin im Slot `sessions`.** Der Default ist `MemorySessions()`. SQLite oder Postgres kommen später als weitere Plugins.
- **Resume repariert automatisch:** Unterbrochene Tool-Calls bekommen ein `interrupted`-Ergebnis, abgebrochene Antworten werden als `interrupted` markiert.
- **Eine Session-ID kann nur ein Prozess gleichzeitig öffnen.** Ein zweiter Versuch wirft `SessionLocked`.

---

## 6. Ausführen: `run`, `RunResult`, Structured Output

```python
result = await session.run("Summarize the open issues.")

result.text  # final assistant text of *this* turn
result.output  # typed output (see below), else None
result.usage  # Usage(input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, cost)
result.stop_reason  # StopReason (StrEnum): END, MAX_TOKENS, MAX_STEPS, STOPPED, ABORTED, DISCARDED
result.turn_id
result.messages  # messages of this turn
result.tool_calls  # (ToolCall, ToolResult) pairs of this turn
```

**Structured Output:**

```python
class Triage(BaseModel):
    severity: Literal["low", "high", "critical"]
    owner: str
    summary: str


result = await session.run("Triage this bug report: ...", output=Triage)
result.output.severity  # typed as Triage
```

**Anhänge:**

```python
await session.run("What's in this picture?", attachments=[Image.from_path("bug.png")])
```

**Ohne Session-Block** (ephemere In-Memory-Session):

```python
result = await agent.run("What is 19 + 23?")
```

---

## 7. Streaming und eigene Events

```python
from harnessed import events

async for event in session.stream("Refactor utils.py"):
    match event:
        case events.text.Delta(text=text):
            print(text, end="", flush=True)
        case events.reasoning.Delta(text=text):
            ui.thinking(text)
        case events.tool.Started(call=call):
            ui.spinner(call.name, call.arguments)
        case events.tool.Progress(call_id=cid, text=text):
            ui.progress(cid, text)
        case events.tool.Finished(call=call, result=result):
            ui.done(call.name, result.status)
        case events.approval.Needed(request_id=request_id):
            await session.approvals.answer(request_id, ApprovalAnswer.ALLOW)
        case events.compaction.Applied(summary=summary):
            ui.summary(summary)
        case events.spill.Created(ref=ref):
            ui.artifact(ref.path)
        case events.turn.Finished(result=result):
            print(result.usage)
```

- Tags sind explizite stabile Literale wie `text.delta`, `reasoning.delta`, `tool.started`
  und `turn.finished`; ein Python-Klassenname bestimmt nicht das Speicherformat.
- `StreamEvent[T]` ist eine Pydantic-kompatible discriminated union der Built-in-UI-Events.
  `EventStream[T]` ergänzt die Basistypen für eigene Events. Klassen-Patterns erhalten den
  präzisen `RunResult[T]`-Typ bei Structured Output.
- Text- und Reasoning-Deltas sind transient. Das Log speichert die vollständige Nachricht.
- Approval-Events enthalten Daten; Antworten laufen über `session.approvals.answer`.
- Der Stream endet mit dem eigenen Turn. Rohes Log und Live-Notifications sind separat über
  `session.subscribe()` bzw. `session.subscribe(transient=True)` abonnierbar.

Eigene Events sind frozen Dataclasses; Pydantic übernimmt Validierung, Union und Schema:

```python
from dataclasses import dataclass, field
from typing import Annotated, Literal
from pydantic import Field, TypeAdapter
from harnessed import Plugin, ToolContext, events, tool


@dataclass(frozen=True, slots=True)
class ProjectIndexed(events.DurableEvent):
    files: int
    type: Literal["project.indexed"] = field(default="project.indexed", kw_only=True)


class Indexer(Plugin):
    events = (ProjectIndexed,)

    @tool(readonly=True)
    async def index(self, *, ctx: ToolContext) -> str:
        await ctx.emit(ProjectIndexed(files=42))
        return "Indexed."


type ProjectEvent = Annotated[
    events.BuiltinEvent | ProjectIndexed, Field(discriminator="type")
]
adapter = TypeAdapter(ProjectEvent)
```

`TransientEvent` statt `DurableEvent` liefert dieselbe Erweiterung ohne Persistenz.
Der Agent sammelt `Plugin.events` vor dem Laden gespeicherter Logs. Die Registry ist
sessionlokal; verschiedene Klassen mit demselben Tag sind ein Konfigurationsfehler.
Runtime-Plugins registrieren ihre Typen vor Setup. Codecs bleiben nach Dispose bekannt,
damit bereits gespeicherte Events lesbar bleiben. Replay führt Tools und Observer nicht
erneut aus. Eigene Datensätze ändern die Core-Zustandsmaschine nicht automatisch.

`events.EventRegistry([...])` bietet `schema()`, `encode()` und `decode()` für persistente
Events. Das laufende Gesamtverzeichnis liegt unter `session.log.registry`. Zum Decodieren
von Pydantic-Endergebnissen mit eigenen Log-Events kann der Adapter diese Registry als
`context={"events": registry}` erhalten. JSONL verwendet ausschließlich Version 1
mit den kanonischen dotted Tags. Unbekannte oder frühere experimentelle Formate
werden abgewiesen; bestehende Logs werden nicht umgeschrieben.

Dynamisch geladene Plugins können eine statische Python-Union nicht nachträglich ändern.
Anwendungen deklarieren ihre konkrete Union daher explizit, wenn sie alle eigenen
Varianten statisch prüfen möchten. Die Registry übernimmt die dynamische Validierung.

---

## 8. Inbox und Run-Control

Inbox und Run-Control sind Core-Mechanismus und kein Plugin, weil sie das Log-Format bestimmen. Alles hier ist durable: Ein Neustart verliert weder die Inbox noch eine Pause.

### 8.1 Nachrichten während eines Turns

Runs werden über `run` oder `submit` eingereiht; `steer` liefert Korrekturen und
`context.set` verändert Fakten ohne einen Turn zu starten:

```python
# while a turn is running, from a different task or the UI:
result = await session.run(
    "And afterwards, write tests."
)  # waits: queued as its own turn after the current one
await session.steer("Use pathlib, not os.path.")  # into the running turn, before the next step
await session.context.set("ci", "CI is red on main.")  # a fact, sent with the next step, never starts one

session.inbox.pending  # tuple of InboxItem (queued runs and steers)
await session.inbox.remove(item.id)
```

| Aufruf | Wohin | Läuft ein Turn? | Session idle? | Gibt zurück |
|---|---|---|---|---|
| `run(text)` / `stream(text)` | eigener, **neuer** Turn | Wird in die Inbox gestellt und startet, wenn alle Turns davor fertig sind. | Startet sofort. | `RunResult` bzw. Events **dieses** Turns |
| `submit(text)` | eigener neuer Turn | Aufnahme wird bestätigt, Ausführung bleibt eingereiht. | Startet sofort. | `RunHandle[T]` |
| `steer(text)` | in den **laufenden** Turn | Wird vor dem nächsten Step als User-Message eingefügt, nachdem die Tool-Calls des aktuellen Steps committed sind. | Startet einen neuen Turn, ohne darauf zu warten. | `InboxItem` |
| `context.set(name, value)` | Kontext (4.4) | Geht mit dem nächsten Step mit. | Geht mit dem nächsten Turn mit. Startet nichts. | `None` |

Regeln:
- **`run()` wartet, statt zu werfen.** Es gibt kein `SessionBusy` und keine eigene Follow-up-Methode. Das entspricht der Queue von Codex (Tab), Claude Code (`queueSubmit`) und LangGraph (`enqueue`). Jeder `run()` bekommt seinen eigenen Turn und sein eigenes `RunResult`, die Ergebnisse vermischen sich also nicht.
- **Ohne auf das Ergebnis zu warten:** `handle = await session.submit(text)`. Der Aufruf
  bestätigt die durable Aufnahme; `await handle.result()` liefert das Ergebnis. Der Handle
  bietet außerdem `id` und `done()`. `run` ist Submit plus Ergebnis-Wait; beide nutzen den
  vorhandenen Session-Driver, ohne pro Handle einen Hintergrund-Task anzulegen.
- **Handle-Lebenszeit:** Wiederholtes `result()` liefert dasselbe typisierte Ergebnis.
  Ein abgebrochener Ergebnis-Wait bricht den Run nicht ab. Driver-Fehler kommen über den
  Handle; beim Schließen scheitern unfertige Live-Handles mit `SessionClosed`, während
  gespeicherte Inputs und pausierte Turns erhalten bleiben.
- **Reihenfolge:** Gestellte Turns laufen in der Reihenfolge ihres Aufrufs. Mehrere `steer()` vor demselben Step werden zusammen und in Reihenfolge eingefügt.
- **In `PAUSED`** nimmt die Inbox weiter an, es startet aber nichts bis `resume()`.
- **Entfernte oder verworfene Turns:** Wird ein gestellter Turn per `inbox.remove()` entfernt oder durch `stop()`/`abort()` verworfen (8.2), bekommt der wartende `run()` ein `RunResult` mit `stop_reason=StopReason.DISCARDED` und keine Exception.
- **`InboxItem`** hat `id`, `kind: InboxKind` (`StrEnum` mit `RUN` und `STEER`), `content` und `created_at`.
- **Kein `inject` mehr.** Fakten ohne Aufwecken sind Kontext (4.4). `Rules` setzt dafür einen benannten Eintrag (`rule:<name>`) und entfernt ihn wieder, wenn die Regel nicht mehr greift. Die Regel-Aktion `steer` bleibt.

**Begriffe:** `steer` verwenden pi (`steer`) und Codex (`turn/steer`) mit genau dieser Bedeutung. Für den Fall, den DSH `followup` nennt, gibt es kein einheitliches Wort (pi: `followUp`, Codex, Claude Code: "queue", LangGraph: `enqueue`), deshalb hat er bei uns keinen eigenen Namen: Es ist einfach ein weiterer `run()`. `interrupt` vermeiden wir, weil es bei Codex und Claude Code *abbrechen* heißt, bei LangGraph und dem OpenAI Agents SDK dagegen *für eine Freigabe anhalten* (8.2).

### 8.2 Run-Control

```python
await session.pause()  # finish the current step, then wait
await session.resume()  # continue with the next step
await session.stop()  # graceful: finish the current step, then end the turn
await session.abort()  # immediately: cancel the model call and tools, repair the log, end the turn
```

| Methode | Was mit dem laufenden Step passiert | Danach | Status währenddessen → danach | Kehrt zurück, wenn … |
|---|---|---|---|---|
| `pause()` | Läuft zu Ende: Model-Antwort und ihre Tool-Calls werden fertig und committed. | Es startet kein neuer Step. Neue Nachrichten landen in der Inbox. | `RUNNING` → `PAUSED` | der Step committed ist |
| `resume()` | – | Der Turn macht mit dem nächsten Step weiter. | `PAUSED` → `RUNNING` | der Loop wieder läuft |
| `stop()` | Läuft zu Ende wie bei `pause()`. | Der Turn endet. `run()` liefert ein `RunResult` mit `stop_reason=StopReason.STOPPED`. | `STOPPING` → `IDLE` | der Turn beendet ist |
| `abort()` | Wird abgebrochen: Der Model-Request und laufende Tools werden per asyncio gecancelt. Offene Tool-Calls bekommen ein `ToolResult` mit Fehler `"aborted"`, damit das Log konsistent bleibt. | Der Turn endet. `run()` liefert `stop_reason=StopReason.ABORTED`. | `STOPPING` → `IDLE` | das Log repariert ist |

Regeln für alle vier:
- **Idempotent.** `pause()` auf einer pausierten Session, `resume()` auf einer laufenden sowie `stop()`/`abort()` auf einer idle Session tun nichts und werfen nicht. Ein UI darf also doppelt klicken.
- **`abort()` gewinnt.** Ein `abort()` während `STOPPING` bricht den laufenden Step sofort ab.
- **Kein Fehler beim Aufrufer.** Wer auf `run()` wartet, bekommt bei `stop()`/`abort()` keine Exception, sondern ein normales `RunResult` mit dem passenden `stop_reason`.
- **Die Inbox.** `stop()` und `abort()` verwerfen wartende Nachrichten, außer mit `keep_inbox=True`. Jede verworfene Nachricht erzeugt ein Log-Event, damit ein UI den Text wiederherstellen kann.
- **Nach einem Neustart:** Eine pausierte Session bleibt `PAUSED`. Eine Session, die beim Absturz `RUNNING` war, wird per Crash-Repair wie nach `abort()` beendet und ist danach `IDLE`.
- **Warum `abort` statt `cancel`:** `stop` und `cancel` klingen gleich, verhalten sich aber sehr unterschiedlich. `abort` sagt klar "sofort, ohne Rücksicht auf den laufenden Step", und pi verwendet es genauso. `interrupt` meiden wir, weil es bei Codex und Claude Code *abbrechen* heißt, bei LangGraph und dem OpenAI Agents SDK aber *für eine Freigabe anhalten*. Freigaben heißen bei uns `Approvals`.

### 8.3 Status und Warten

```python
from harnessed import SessionStatus

session.status is SessionStatus.RUNNING
await session.wait_until_idle()  # until IDLE and the inbox is empty
async with asyncio.timeout(30):
    await session.wait_until_idle()
```

```python
class SessionStatus(StrEnum):
    IDLE = "idle"  # no turn running, inbox empty or waiting for the next turn
    RUNNING = "running"  # a turn is running
    PAUSED = "paused"  # a turn is paused between two steps
    STOPPING = "stopping"  # stop() or abort() is in progress
    CLOSED = "closed"  # the session is closed; every method raises SessionClosed
```

| von → nach | Auslöser |
|---|---|
| `IDLE` → `RUNNING` | `run()`, `stream()`, `steer()` oder ein Turn aus der Inbox |
| `RUNNING` → `IDLE` | Der Turn endet regulär. |
| `RUNNING` → `PAUSED` | `pause()` |
| `PAUSED` → `RUNNING` | `resume()` |
| `RUNNING`/`PAUSED` → `STOPPING` | `stop()` oder `abort()` |
| `STOPPING` → `IDLE` | Der Turn ist beendet. |
| beliebig → `CLOSED` | Die Session wird geschlossen (`async with` endet oder `close()`). Ein laufender Turn wird vorher wie mit `abort()` beendet. |

- `session.status` ist ein `SessionStatus` und kein String. Durch `StrEnum` funktionieren Vergleiche mit `"idle"` und JSON-Serialisierung trotzdem.
- **`wait_until_idle()`** kehrt zurück, sobald der Status `IDLE` ist **und** die Inbox leer ist, also wenn alles abgearbeitet ist. Wartet die Session gerade auf `PAUSED`, kehrt die Methode erst nach `resume()` zurück. Wird die Session geschlossen, wirft sie `SessionClosed`. Ein Timeout kommt bewusst von außen über `asyncio.timeout`, es gibt keinen eigenen Parameter.
- **Warum `wait_until_idle` statt `when_idle`:** Der Name sagt, was der Aufruf tut. `settle()` würde zwar zum Kernel passen, bedeutet dort aber etwas anderes ("alle Abhängigkeiten aufgelöst").
- Jeder Statuswechsel ist zusätzlich ein Stream-Event `StatusChanged(old, new)`, damit ein UI nicht pollen muss.

---

## 9. Approvals

```python
from harnessed import ApprovalAnswer, ApprovalRequest, PolicyDecision
from harnessed.plugins import Approvals, Filesystem


async def approve(request: ApprovalRequest) -> ApprovalAnswer:
    print(request.call.name, request.call.arguments)
    return ApprovalAnswer.ALLOW


agent = Agent(
    model=model,
    plugins=[
        Filesystem(root=workspace),
        WorkspaceTools(),
        FailureNudge(threshold=2),
        Approvals(
            default=PolicyDecision.ALLOW,
            require=[WorkspaceTools.count_lines],
            approver=approve,
        ),
    ],
)
```

| Reihenfolge | Regel | Entscheidung |
|---|---|---|
| 1 | in `deny` | DENY, ohne Nachfrage |
| 2 | in `require` | ASK, auch bei readonly, Trust oder erlaubender Policy |
| 3 | `policy(call)` liefert einen Wert | diese PolicyDecision |
| 4 | in `trust` oder readonly | ALLOW |
| 5 | übrige Tools | `default`, zunächst PolicyDecision.ASK |

- `default=PolicyDecision.ALLOW` ersetzt den String-Sentinel für vollständiges Vertrauen.
- Tool-Referenzen kommen von dekorierten Klassenmethoden oder gebundenen Plugin-Rezepten.
  Sie erhalten `@tool(name=...)`, prüfen die aktive Deklaration und scheitern bei fehlenden
  Tools am Session-Start. String-Namen bleiben für dynamische Integrationen möglich.
- Callbacks und strukturelle `Approver.ask(request)` erhalten dieselben ApprovalRequest-Daten.
  Normale Aufrufer brauchen weder Service-Wrapper noch CallbackApprover.
- `ALLOW` gilt für den Aufruf; `ALWAYS` erzeugt einen durablen Grant pro Tool und Scope.
  `await session.approvals.revoke(request_id)` widerruft ihn; Forks widerrufen standardmäßig.
- Ohne Callback oder aktiven UI-Responder liefert ASK sofort ein unavailable-Ergebnis.
  `Approvals(timeout=...)` begrenzt die Antwortzeit auf einen positiven endlichen Wert.
- UI-Antworten erfolgen über `await session.approvals.answer(request_id, decision)` und
  warten auf das Persistenz-Acknowledgement. Identische Antworten sind idempotent.
- Unabhängige harte Guards bleiben über `ApprovalGate(risky=[WorkspaceTools.count_lines])`
  und den öffentlichen `ToolPolicy`-Hook komponierbar.

---

## 10. Kontextmanagement

```python
from harnessed.plugins import Compaction, Spill, ToolOutputLimit, at_ratio, every_turns

agent = Agent(
    model=...,
    plugins=[
        ToolOutputLimit(max_chars=16_000),  # replaces the default (32k)
        Spill(threshold_tokens=8_000),  # large results -> file + read_spill tool
        Compaction(
            trigger=at_ratio(0.8),  # or every_turns(10), or any callable
            keep_turns=2,
            summarizer=None,  # None = the Agent's model
            focus="Keep file paths, decisions and open TODOs.",  # hint for the summarizer
        ),
    ],
)
```

- Das Kontextfenster kommt aus `model.info.context_window` (3.3).
- Meldet der Provider `ContextLengthExceeded` und ist `Compaction` installiert, wird einmal compacted und der Request wiederholt.
- Eigener Trigger:

```python
def trigger(state: ContextState) -> bool:
    return state.ratio > 0.7 or state.turns_since_compaction > 20


Compaction(
    trigger=trigger, summarizer="deepseek:deepseek-chat"
)  # cheaper model, same forms as model=
```

- Manuell compacten: `await session.compact()`. Der Mechanismus ist Core, nur das *Wann* ist ein Plugin.
- Spill-Dateien gehören zur Session. Sie liegen bei der Persistenz und werden mit der Session gelöscht.

---

## 11. Subagents (später)

**Zurückgestellt:** Diese Erweiterung ist nicht Teil der aktuellen Umsetzung. Die
folgende Skizze dokumentiert nur das mögliche spätere Design; die gezeigte API ist
nicht verfügbar. Für diese Iteration sind weder ein Subagent-Plugin noch
Kind-Session-Verwaltung oder geerbte Parent-Approvals vorgesehen.

Ein späterer Subagent wäre ein Plugin, das genau ein Tool beisteuert:

```python
from harnessed.plugins import Subagent

researcher = Agent(
    model="deepseek:deepseek-chat",
    instructions="Research thoroughly, answer with sources.",
    plugins=[WebSearch()],
)

lead = Agent(
    model="anthropic:claude-opus-5-5",
    plugins=[
        Subagent(
            researcher,
            name="research",
            description="Delegate a research question.",
            output=ResearchReport,  # optional structured output
        ),
    ],
)
```

- Jeder Aufruf erzeugt eine **Kind-Session** mit eigenem Log und Parent-Referenz, die sich nachträglich inspizieren lässt (`session.children`).
- Approvals der Kind-Session laufen über den Approver des Parents.
- Der Stream des Parents zeigt `ToolProgress` mit den Zwischenschritten des Kinds.

---

## 12. Skills

Ein Skill ist ein Ordner mit einer `SKILL.md` im offenen [Agent-Skills-Format](https://agentskills.io): Frontmatter mit `name` und `description`, danach Anweisungen in Markdown, optional weitere Dateien und Skripte. Harnessed liefert dafür eine Referenzimplementierung als normales Plugin, `Skills`. Es nutzt nur öffentliche API (Regel 5) und lässt sich also ersetzen.

```
skills/
  pdf-forms/
    SKILL.md            # frontmatter + instructions
    reference.md        # optional, read on demand
    scripts/fill.py     # optional, run via Shell
  release-notes/
    SKILL.md
```

```markdown
---
name: pdf-forms
description: Fill and validate PDF forms. Use when the user asks to fill out or check a PDF form.
---
# PDF forms

1. List the fields with `python scripts/fill.py --list <file>`.
2. For field types, read `reference.md`.
```

```python
from harnessed.plugins import Filesystem, Shell, Skill, Skills

agent = Agent(
    model="anthropic:claude-sonnet-5-5",
    plugins=[
        Skills(
            "./skills",  # folder of skill folders
            "~/.harnessed/skills",  # earlier entries win on name clashes
            Skill.from_dir("./vendor/pdf-forms"),  # a single skill folder
            Skill(  # inline, without a folder (tests, DB, ...)
                name="release-notes",
                description="Write release notes. Use when asked for a changelog or release notes.",
                instructions="1. Collect merged PRs since the last tag. ...",
            ),
        ),
        Filesystem(),
        Shell(),
    ],
)
```

### 12.1 Wie ein Skill geladen wird

Das Modell lädt einen Skill stufenweise und sieht immer nur so viel, wie es gerade braucht:

| Stufe | Was | Wann | Wie |
|---|---|---|---|
| 1 · Katalog | `name` und `description` jedes Skills | immer | als `guidance` von `Skills` im System-Prompt (4.1) |
| 2 · Anweisungen | Rumpf der `SKILL.md` | wenn das Modell den Skill braucht | Tool `load_skill(name)` |
| 3 · Dateien | weitere Dateien im Skill-Ordner | bei Bedarf | Tool `read_skill_file(name, path)` |
| – · Skripte | `scripts/…` | bei Bedarf | über `Shell`, nicht über `Skills` |

Der Katalog ist die `guidance` von `Skills` und landet nach der normalen Regel an der Position von `Skills` in `plugins=`:

```
Skills are folders with instructions for specific tasks. Before starting a task that matches a skill's description, call load_skill with its name and follow the instructions.

<skills>
<skill name="pdf-forms">Fill and validate PDF forms. Use when the user asks to fill out or check a PDF form.</skill>
<skill name="release-notes">Write release notes. Use when asked for a changelog or release notes.</skill>
</skills>
```

- **`load_skill(name)`** liefert den Rumpf der `SKILL.md` ohne Frontmatter, den absoluten Pfad des Skill-Ordners und die Liste seiner Dateien. Die Datei wird bei jedem Aufruf frisch gelesen.
- **`read_skill_file(name, path)`** liest eine Datei im Skill-Ordner. Ein Pfad außerhalb des Ordners führt zu einem `ToolError`. So funktionieren Skills auch in Agents ohne `Filesystem`.
- **Beide Tools sind `readonly=True`** und laufen daher ohne Rückfrage (9). Ihre Ausgabe wird nie gekürzt oder ausgelagert (`output_limit=None`), sonst sähe das Modell nur halbe Anweisungen.
- **`Skills` führt nichts aus.** Skripte laufen über `Shell` und unterliegen dessen Approvals. Ohne `Shell` kann das Modell Skripte nur lesen. `Skills` importiert weder `Filesystem` noch `Shell`.

### 12.2 Regeln

- **Quellen sind explizit.** `Skills` sucht nirgends von selbst.
  - Ein Pfad (`str` oder `Path`) ist ein Ordner. Jeder **direkte** Unterordner mit einer `SKILL.md` ist ein Skill; es wird nicht rekursiv gesucht.
  - Einen einzelnen Skill-Ordner übergibt man mit `Skill.from_dir(...)`.
- **Bei Namensgleichheit gewinnt der frühere Eintrag**, wie bei `PATH`. Projekt-Skills, die vor den Nutzer-Skills stehen, verdecken also gleichnamige Nutzer-Skills. `agent.prompt` zeigt, welcher Skill welchen verdeckt.
- **Validierung nach dem Format:**
  - `name` hat 1–64 Zeichen aus Kleinbuchstaben, Ziffern und Bindestrichen und entspricht dem Ordnernamen.
  - `description` ist nicht leer und hat höchstens 1024 Zeichen.
  - Verstöße, fehlende Ordner und kaputte Frontmatter führen zu einem `ConfigError` mit Pfad. Ein kaputter Skill wird nie still übersprungen.
- **Weitere Frontmatter-Felder** (`license`, `compatibility`, `metadata`) landen unverändert in `Skill.metadata` und werden nicht ausgewertet. `allowed-tools` wird vorerst ignoriert (offen, siehe 20).
- **Der Katalog wird beim Start jeder Session gelesen**, wie `Section.from_file`. Neue Skill-Ordner erscheinen also in neuen Sessions, ohne dass man den Agent neu baut. Innerhalb einer Session bleibt der Katalog fest, damit der Prompt-Cache hält.
- **Geladene Skills sind normale Tool-Ergebnisse im Log.** Sie sind also durable und nach einem Resume noch da.
- **Compaction:** Ein geladener Skill überlebt eine Compaction. Dafür setzt `Skills` danach für jeden zuvor geladenen Skill einen Kontext-Eintrag `skill:<name>` mit dessen Anweisungen (4.4), so wie `Rules` es mit `rule:<name>` macht.
- **Die Frontmatter ist YAML.** Den Parser bringt das Extra `harnessed[skills]` (`pyyaml`) mit. Fehlt das Extra, wirft `Skills(...)` einen `ConfigError` mit dem genauen Installationsbefehl, wie bei den Models (3.2). Ein Inline-`Skill(...)` braucht das Extra nicht.

```python
>>> print(agent.prompt)
  ...
  3. "Skills are folders with…"   Skills.guidance (plugins[0]), 2 skills
       pdf-forms       ./skills/pdf-forms   (shadows ~/.harnessed/skills/pdf-forms)
       release-notes   inline
```

---

## 13. MCP (später)

Für jetzt nicht geplant. Ein MCP-Client wäre später ein Tool-Plugin wie jedes andere, mit eigener Extra-Dependency. Er öffnet die Verbindung in `setup`, registriert die Tools des Servers als normale Tools und schließt die Verbindung beim Dispose. Weil Tools ohnehin aus Plugins kommen, passt das ohne Breaking Change dazu.

---

## 14. Plugins mit Hooks (Ebene 2)

**Wann man Hooks braucht:** wenn man das Verhalten des Loops ändern will, also Requests umschreiben, Tool-Ergebnisse nachbearbeiten, eigene Policies umsetzen oder auf Log-Events reagieren. Hooks und Tools können im selben Plugin stehen, es gibt nur ein Plugin-Konzept.

### 14.1 Funktional (kleine Dinge)

```python
from harnessed import Context, ToolResult, plugin
from harnessed.hooks import Next, ReviewToolResult


@plugin
async def redact_secrets(ctx: Context, *, patterns: list[str] = DEFAULT_PATTERNS):
    regex = re.compile("|".join(patterns))

    @ctx.hook(ReviewToolResult)
    async def redact(event: ReviewToolResult, next_: Next[ToolResult]) -> ToolResult:
        result = await next_()
        return result.with_text(regex.sub("[REDACTED]", result.text))

    yield  # plugin active until here; teardown after yield


agent = Agent(..., plugins=[redact_secrets(patterns=[r"sk-\w+"])])  # IDE knows `patterns`
```

### 14.2 Klassenbasiert (Hooks und Tools zusammen)

```python
from pydantic import PositiveInt
from harnessed import Plugin, PluginConfig, PolicyDecision, events, tool
from harnessed.kernel import Context, hook
from harnessed.hooks import Next, ToolPolicy


class RateLimitConfig(PluginConfig):
    max_calls_per_turn: PositiveInt = 20


class RateLimit(Plugin):
    Config = RateLimitConfig
    config: RateLimitConfig

    def __init__(self, max_calls_per_turn: int = 20):
        super().__init__(max_calls_per_turn=max_calls_per_turn)

    async def setup(self, ctx: Context) -> None:  # per mount; state lives here only
        self.calls = 0

    @hook(events.turn.Started)  # react to log events
    async def reset(self, event: events.turn.Started) -> None:
        self.calls = 0

    @hook(ToolPolicy)  # hook: may decide
    async def limit(self, event: ToolPolicy, next_: Next[PolicyDecision]) -> PolicyDecision:
        self.calls += 1
        if self.calls > self.config.max_calls_per_turn:
            return PolicyDecision.DENY
        return await next_()

    @tool(readonly=True)  # same plugin, also a tool
    async def remaining_calls(self) -> int:
        """How many tool calls are left in this turn."""
        return self.config.max_calls_per_turn - self.calls


agent = Agent(..., plugins=[RateLimit(max_calls_per_turn=10)])  # validated by Config
```

Ein eigenes Plugin kann auch einen Default ersetzen, indem es denselben Slot belegt:

```python
class PostgresSessions(Plugin, slot="sessions"): ...
```

### 14.3 Hook-Katalog (`harnessed.hooks`)

| Hook | Semantik | Wofür |
|---|---|---|
| `BeforeStep` | emit | vor dem Request-Bau: Compaction, Budget-Checks |
| `PrepareRequest` | waterfall → `ModelRequest` | Request umschreiben (Messages, Settings, Tools filtern) |
| `ToolPolicy` | waterfall → `PolicyDecision` | ALLOW, DENY oder ASK pro Call |
| `RequestApproval` | bail → `ApprovalAnswer` | eigener Approver als Plugin |
| `ExecuteTool` | waterfall → `ToolResult` | Ausführung ersetzen oder umschließen (Sandbox, Cache, Mock) |
| `ReviewToolResult` | waterfall → `ToolResult` | Ergebnis nachbearbeiten (Redaction, Spill, Limits) |
| `AfterStep` | emit | Metriken, Regeln, Steering |
| `PluginFailed` | emit | Alerting, Restart |
| `SessionAppended` | emit | nach jedem angehängten Log-Event (mit `seq`): Spiegelung, Indizes, Audit |

Daneben ist jedes **Log-Event** aus `harnessed.events` als `@hook(events.X)` abonnierbar, immer nur beobachtend.

**Signaturen sind vollständig typisiert.** Die Semantik des Hooks bestimmt die Signatur:

| Semantik | Signatur |
|---|---|
| emit / Log-Event | `async def f(event: E) -> None` |
| bail | `async def f(event: E) -> R \| None` |
| waterfall | `async def f(event: E, next_: Next[R]) -> R` |

- `Next[R]` ist `Callable[[], Awaitable[R]]` und darf nur einmal aufgerufen werden.
- `R` kommt aus dem Hook selbst (`ReviewToolResult` ist ein `Waterfall[ToolResult]`).
- `@hook` und `ctx.hook` sind generisch über `E` und `R`. Der Typechecker meldet deshalb einen falschen `event`-Typ oder einen falschen Rückgabetyp schon im Editor.
- Weitere Parameter nach `event`/`next_` sind `Inject[T]`.

### 14.4 Reihenfolge

```python
from harnessed.hooks import Priority


@hook(ToolPolicy, priority=Priority.GUARD)     # runs first (security)
@hook(ToolPolicy)                              # Priority.DEFAULT
@hook(AfterStep, priority=Priority.OBSERVE)    # runs last (metrics)
```

### 14.5 Services und Abhängigkeiten

```python
class Metrics(Plugin):
    async def setup(self, ctx: Context) -> None:
        client = MetricsClient(...)
        ctx.provide(MetricsClient, client)  # service for everyone else
        ctx.effect(client.aclose)  # closed on dispose


class SlowToolAlert(Plugin):
    @hook(events.ToolFinished)
    async def check(
        self,
        event: events.ToolFinished,
        metrics: Inject[MetricsClient | None],  # optional
    ) -> None:
        if metrics and event.duration > 10:
            metrics.incr("slow_tool", tool=event.call.name)
```

- **`Inject[T]`** ist hart: Das Plugin wartet, bis `T` da ist, und startet neu, wenn `T` ausgetauscht wird.
- **`Inject[T | None]`** ist optional: Der Wert ist `None`, solange `T` fehlt, und das Plugin startet deshalb nicht neu.
- **`ctx.on(Event, fn)` / `ctx.hook(...)`** ohne Decorator liefert einen Disposer.
- `Service(T, instance)` aus 2.6 ist genau so ein Plugin, nur ohne Lebenszyklus.

---

## 15. Observability

**Jetzt:** Logging nach Bibliothekskonvention. Harnessed konfiguriert nichts selbst und nutzt nur `logging.getLogger("harnessed.*")`.

```python
import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("harnessed.tools").setLevel(logging.DEBUG)
```

**Jetzt, aber nur als Seam:** Der Slot `tracer` mit `NoopTracer()` als Default. Der Loop öffnet Spans an festen Stellen. Wer heute schon Tracing braucht, schreibt ein Plugin für den Slot:

```python
class MyTracer(Plugin, slot="tracer"):
    async def setup(self, ctx):
        ctx.provide(Tracer, MyTracerImpl())  # one method: span(name, **attrs)
```

| Span | Attribute (Auszug) |
|---|---|
| `harnessed.turn` | `session.id`, `turn.id`, `stop_reason` |
| `harnessed.step` | `step`, `compacted` |
| `harnessed.model` | `model`, `input_tokens`, `output_tokens`, `cache_read_tokens` |
| `harnessed.tool` | `tool`, `status`, `duration` |
| `harnessed.approval.wait` | `tool`, `answer` |
| `harnessed.compaction` | `before_tokens`, `after_tokens` |

**Später (Erweiterung):** ein `OtelTracer`-Plugin als Extra `harnessed[otel]`, das diese Spans auf die OpenTelemetry GenAI Semantic Conventions abbildet. Weil Slot und Span-Punkte schon existieren, kommt es ohne Breaking Change dazu. Für die Model-Requests selbst kann es die Instrumentierung von pydantic-ai nutzen (3.5), statt sie nachzubauen.

---

## 16. Fehler

Alle Fehler kommen aus `harnessed.errors` (Ebene 1). Der Baum lebt in `core/errors.py`;
die Facade re-exportiert ihn explizit, analog zu `harnessed.hooks`.

```
HarnessedError
├── ConfigError                 # invalid options / plugin config (Pydantic ValidationError inside)
│   ├── PluginConflict          # two plugins in the same slot
│   └── ToolNameConflict        # two live tools with one name
├── ApprovalUnavailable         # ASK without a reachable approver
├── SessionLocked
├── SessionStateError           # operation invalid in the current status
│   └── SessionClosed
├── SessionFormatError          # stored record does not match the log format
├── ContextBudgetExceeded       # message names Compaction(...)
├── OutputTokenLimit            # on_max_tokens=FAIL
├── OutputValidationError       # structured output retries exhausted
├── OutputUnavailable
├── StreamOverflow              # slow stream consumer
└── ProviderError
    ├── AuthError
    ├── RateLimited             # .retry_after
    ├── ContextLengthExceeded
    ├── InvalidRequest
    └── ProviderUnavailable
ToolError                       # raised *by* tools, text goes to the model
```

---

## 17. Testen

```python
from harnessed.testing import ScriptedModel, InMemoryFS, FakeShell, assert_tool_called, reply, call


async def test_adds_numbers():
    model = ScriptedModel(
        [
            call("add", a=19, b=23),
            reply("The result is 42."),
        ]
    )
    agent = Agent(model=model, plugins=[Calculator()])

    result = await agent.run("What is 19 + 23?")

    assert result.text == "The result is 42."
    assert_tool_called(result, "add", a=19, b=23)
    assert model.requests[1].messages[-1].text == "42"
```

**Tools lassen sich auch ohne Agent testen**, weil sie normale Methoden sind:

```python
async def test_add_directly():
    assert await Calculator().add(19, 23) == 42
```

**Fakes sind Plugins** und ersetzen die echten über denselben Slot:

```python
agent = coding(
    model=ScriptedModel([...]),
    plugins=[InMemoryFS({"a.py": "x = 1"}), FakeShell({"pytest": "1 passed"})],
)
```

**pytest-Plugin:**

```python
def test_coding_agent(harnessed_session):  # fixture from harnessed.testing
    session = harnessed_session(coding(...), script=[...])
```

---

## 18. Presets

```python
from harnessed.presets import coding

agent = coding(
    model="anthropic:claude-sonnet-5-5",
    workspace=".",
    plugins=[Approvals(approver=ask_in_terminal, trust=["read_file", "glob", "grep"])],
)
```

- Ein Preset ist **nur eine Funktion, die einen `Agent` zurückgibt**, also eine vorbefüllte Plugin-Liste: `Filesystem` (`read_file`, `list_dir`, `glob`, `grep`, `edit`, `write_file`), die gehärtete `Shell` (`run_command`, ASK per Default), `Spill` und `Compaction(at_ratio(0.8))`.
- Die `instructions` des Presets sind `[coding.PERSONA, Section.from_file("AGENTS.md")]`. Danach folgt die `guidance` von `Filesystem` und `Shell` nach der normalen Regel (4.1). Eigene `instructions=` ersetzen die Preset-Liste vollständig.
- Eigene `plugins=` werden angehängt. Belegt ein Plugin denselben Slot wie eines aus dem Preset, ersetzt es dieses, so wie oben `Approvals`.
- Skills sind nicht im Preset, weil es keinen Standardpfad gibt. `plugins=[Skills("./skills")]` hängt sie an (12).
- `coding(...).plugins` zeigt, was drin ist. Ein Preset ist damit nie eine Black Box.

---

## 19. Kernel (Ebene 3)

Für alle, die einen eigenen Loop oder ein eigenes Framework bauen. `Agent` ist intern genau das hier: ein Context, in den die Plugin-Liste installiert wird.

```python
from harnessed.kernel import Context, Key, needs

DB = Key[Database]("db")


async with Context() as root:
    root.provide(DB, Database(...))

    @root.plugin(needs=[DB])
    async def indexer(ctx: Context, db: Inject[DB]):
        task = ctx.spawn(index_forever(db))  # cancelled automatically on dispose
        yield

    await root.settle()  # wait for all needs to resolve, raise setup errors
```

---

## 20. DX-Entscheidungen

| Frage | Status | Entscheidung |
|---|---|---|
| Wie viel Zucker hat `Agent`? | entschieden | **Drei Parameter** (`model`, `instructions`, `plugins`). Jeder entspricht 1:1 einem Plugin. |
| Lose Funktionen als Tools (`tools=[fn]`)? | entschieden | **Nein.** Tools sind immer `@tool`-Methoden in einem Plugin. Das ist idiomatisch für „everything is a plugin“, bündelt zusammengehörige Tools mit Config und Lebenszyklus und lässt genau einen Registrierungsweg übrig. Der Preis ist eine Klasse mehr im Hello World. |
| Wie entsteht der System-Prompt? | entschieden | **Feste Regel:** erst `instructions`, dann die `guidance` jedes Plugins in der Reihenfolge von `plugins=`, wörtlich, mit Leerzeilen getrennt. Überschreibbar durch eigene Platzierung, `guidance=None` oder `guidance="..."`. `agent.prompt` zeigt die Herkunft jeder Zeile. |
| Order-Zahlen für Sections wie in DSH? | entschieden | **Nein.** Bei Plugins von Dritten kollidieren sie. Die Reihenfolge von `plugins=` reicht. |
| Dynamische Sections im System-Prompt? | entschieden | **Nein.** Was sich ändert, ist Kontext (4.4), damit der Prompt-Cache hält. |
| Policies als Parameter (`compaction=`) oder als Plugins? | entschieden | **Plugins.** Nur so bleiben Built-ins und Plugins von Dritten gleichrangig. Auffindbar sind sie über `harnessed.plugins`. |
| Eigenes `Toolset`-Konzept? | entschieden | **Nein.** Ein Plugin mit `@tool`-Methoden ist genau das. |
| Services als Dict (`services={T: obj}`)? | entschieden | **Nein**, sondern das Plugin `Service(T, obj)`. |
| Wie ersetzt man Defaults? | entschieden | **Slots.** Gleicher Slot ersetzt, doppelter Slot in `plugins=` ist ein Fehler. |
| Wie heißen Model und Adapter? | entschieden | Protokoll **`Model`**, Parameter **`model=`**. Provider kommen über **pydantic-ai** als optionale Integration (Extras `harnessed[openai]`, `harnessed[anthropic]`, … auf `pydantic-ai-slim`). Es gibt genau einen Adapter, `PydanticAIModel`, und **keine eigenen Provider-Klassen**. `model=` nimmt einen pydantic-ai-String (`"deepseek:deepseek-chat"`), ein pydantic-ai-Model oder ein eigenes `Model`. Settings sind die `ModelSettings` von pydantic-ai, Retries macht pydantic-ai. `StreamingLLM`, `RetryPolicy` und der eigene httpx-Adapter entfallen (3.1–3.5). |
| Run-Control-Verben? | entschieden | `pause`, `resume`, `stop` (graceful), `abort` (sofort). `cancel` entfällt, weil es neben `stop` nicht unterscheidbar ist. Alle vier sind idempotent (8.2). |
| Status und Warten? | entschieden | `session.status` ist ein `SessionStatus`-`StrEnum` mit festen Übergängen. `wait_until_idle()` ersetzt `when_idle()` (8.3). |
| Nachrichten während eines Turns? | entschieden | `run()`/`stream()` warten, wenn die Session beschäftigt ist, und bekommen einen eigenen Turn (kein `followup`, kein `SessionBusy`). `steer()` geht in den laufenden Turn. `inject` entfällt zugunsten von `session.context.set()` (8.1). |
| Schreibweise mitgelieferter Plugins? | entschieden | **Alle mitgelieferten Plugins sind Klassen** und damit CapWords: `AgentLoop`, `MemorySessions`, `JsonlSessions`, `Subagent`, `Service`, `ContextValue`, `Instructions`, `NoopTracer`. Die Bauart eines Plugins schlägt so nicht mehr auf seinen öffentlichen Namen durch. Die Funktionsform mit `@plugin` bleibt für Services und Hooks; Tools sind Methoden. |
| Plugin hinter `model=`? | entschieden | Keine eigene Klasse: `model=x` ist Zucker für `Service(Model, x)` (3.1). |
| Skills? | entschieden | **Referenzimplementierung als Plugin `Skills`** im offenen Agent-Skills-Format (`SKILL.md`). Der Katalog ist die `guidance`, Anweisungen kommen über `load_skill`, Dateien über `read_skill_file`, Skripte nur über `Shell`. Quellen sind explizit, der frühere Eintrag gewinnt, und ein kaputter Skill ist ein `ConfigError` (12). |
| YAML für die Skill-Frontmatter? | offen | **Empfehlung: Extra `harnessed[skills]` mit `pyyaml`**, analog zu den Models. Alternativen: `pyyaml` als Kernabhängigkeit (einfacher, aber eine Abhängigkeit mehr für alle) oder ein eigener Mini-Parser (fragil bei `metadata`). |
| `allowed-tools` aus der Skill-Frontmatter? | offen | Das Feld ist im Format noch experimentell. **Empfehlung: vorerst ignorieren.** Später ließe es sich auf `Approvals` abbilden: Die genannten Tools laufen ohne Rückfrage, solange der Skill geladen ist. |
| Skills vom Nutzer auslösen (`/pdf-forms`)? | später | Apps können dafür schon `session.steer()` oder `session.context.set("skill:<name>", ...)` nutzen. Eine eigene API kommt erst, wenn sich ein Muster zeigt. |
| MCP-Client? | später | Nicht im ersten Release. Er käme als Tool-Plugin mit Extra-Dependency, ohne Breaking Change. |
| OTel-Exporter? | später | Jetzt nur Logging und der `tracer`-Slot. `OtelTracer` kommt als Extra, ohne Breaking Change. |
| `agent.run()` ohne Session-Block? | offen | **Empfehlung: ja.** Das macht Skripte und Tests einzeilig. |
| Sync-Callbacks überall erlauben? | offen | **Empfehlung: ja** für Tools, Instructions und Approver. Synchron wird direkt aufgerufen, nur Tools laufen in einem Thread. |

## Ergänzung: idiomatische Konfiguration

Alle mitgelieferten Plugin-Config-Modelle stehen auf Modulebene, etwa `AgentLoopConfig`.
Die Klasse deklariert `Config = AgentLoopConfig` und `config: AgentLoopConfig`.
Explizit typisierte Konstruktoren geben Aufrufern normale Python-Signaturen und
IDE-Vervollständigung; Pydantic validiert Grenzen, Literale und externe Werte.
Kleine Plugins benötigen keine eigene Config-Klasse, wenn ein normaler Konstruktor reicht.

## Verbindliche Python-Stilentscheidungen

Plugins bleiben Unterklassen von `Plugin`; Abhängigkeiten können als
`class WorkspaceTools(Plugin, needs=(FileSystemBackend,))` deklariert werden.
Der Dateisystem-Baustein heißt `Filesystem(root=...)`, das Backend-Protokoll
`FileSystemBackend`, die lokale Implementierung `LocalFileSystem`.

Geschlossene Zustände und Policies verwenden `StrEnum`; Discriminators behalten
`Literal[Enum.MEMBER]`. Event-Tags sind dotted, etwa `assistant.message`,
`tool.progress` und `step.end`. Eigene Plugins dürfen eigene Discriminator-Enums
verwenden. Es gibt keine Migrationen früherer unveröffentlichter Formate.

Für persistierte strukturierte Antworten ist
`OutputContract("support.triage.v1", Triage)` die explizite versionierbare Form.
`output=Triage` bleibt der einfache Weg. Wiederaufnahme braucht die registrierte
Identität und ein passendes Schema; ein gleiches Schema allein identifiziert
keine Python-Validierungslogik.
