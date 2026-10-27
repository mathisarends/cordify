# Cordify

Ein natives Python-SDK für einen Agent-Harness: Context, Services, Plugins,
typisierte Events, Session-Log und ein austauschbarer ReAct-Loop. Inspiriert von
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) und
[Cordis](https://github.com/cordiverse/cordis), mit deren Begriffen und Python-APIs.
Keine API- oder Dateiformat-Kompatibilität mit den TypeScript-Projekten.

Python **3.12–3.14**, PEP-695-Generics, keine Runtime-Abhängigkeiten im Kern.
Das Paket liegt direkt in `cordify/`.

## Paketstruktur

```text
cordify/
  kernel/                Context, Keys, DI, Events, Effekte, Plugin-Lifecycle
    context.py
    plugin.py
    declarations.py      Agent-unabhängige Plugins, Bundles, Registration-Rezepte
    events.py
    effects.py
    di.py
    keys.py
  seams/                 Protocols und unveränderliche Datenverträge
    llm.py, fs.py, shell.py, approver.py
    tokens.py, compaction.py, spill.py, models.py
  core/                  Agent-Invarianten und öffentliche Core-Events
    loop.py, session.py, inbox.py, approvals.py, tools.py, scheduling.py, prompt.py, events.py
    features.py          Tool-Deklarationen als Kernel-Registrierungen
    attachments.py, execution.py, budget.py, compaction.py, outputs.py, turns.py
  plugins/
    context/             Instructions, Observations, Compaction, Spill
    tools/               Datei- und Shell-Tools
    permissions/         Approvals und Rules
    agents/              Registrierung des Standard-Loops
    observe/             Token-Zählung und Cache-Usage; kein OTel
    services.py          Registry-, Prompt- und LLM-Service-Provider
    persistence.py       JSONL-Plugin
  adapters/              DeepSeek, lokale FS/Shell, JSONL, Spill, Counter,
                         Compactor und deterministischer Replay-Adapter
    base.py              Optionale BaseLLM mit RetryPolicy
    sse.py               Gemeinsames SSE-Data-Framing für Streaming-Adapter
  presets.py             Harness, minimal(), coding(), headless()
  testing.py             Test-Helfer
  __init__.py            Kompakte öffentliche API
```

Die Import-Richtung ist **kernel ← seams ← core ← plugins/adapters ← presets**.
Architekturtests prüfen die Grenzen. `cordify.kernel` lädt keine Agents,
Adapter oder Drittanbieterpakete. Der Kernel kennt generische Registrierungs-
Rezepte; die ToolRegistry-Anbindung entsteht erst in `core.features`.
`kernel.Plugin` ist die allgemeine Basis, `cordify.Plugin` ergänzt die
Agent-Komfort-API mit `tools=[...]` und `.tool`-Bundles.

### Seams verwenden Protocol

`FS`, `LLM`, `Shell`, `Approver`, `TokenCounter`, `Compactor` und `SpillStore`
sind schmale `typing.Protocol`-Verträge. Das gilt auch für die internen Verträge
`AgentFactory` und `SessionPersistence`. Implementierungen erfüllen die Methoden
strukturell und benötigen keine Cordify-Basisklasse:

```python
from pathlib import Path


class MemoryFiles:
    def __init__(self):
        self.files: dict[str, str] = {}

    async def read(self, path: Path) -> str:
        return self.files[str(path)]

    async def write(self, path: Path, text: str) -> None:
        self.files[str(path)] = text


# Erst die Integration kennt Cordify; die Implementierung oben bleibt unabhängig.
from cordify import Context, FS

async with Context() as ctx:
    ctx.provide(FS, MemoryFiles())
    await ctx[FS].write(Path("note.txt"), "hello")
```

Typprüfer vergleichen die Methodensignaturen. Die DI registriert Implementierungen
explizit unter dem Protocol-Key und verlangt keine nominale Vererbung oder
`isinstance`-Prüfung gegen den Seam. Gemeinsame Implementierungslogik gehört in
Adapter oder Plugins; die Datenverträge bleiben unveränderliche Werttypen.
Die Tests führen fremde FS-, LLM-, Shell- und Approver-Implementierungen ohne
Vererbung durch die echte DI-, Approval- und Tool-Pipeline.
Für Adapter mit gemeinsamer Implementierungslogik gibt es zusätzlich eine
optionale `BaseLLM`; der gespeicherte Service-Key bleibt `LLM`.

Die alten flachen Modulpfade und `cordify.std` sind entfernt. Root-Imports wie
`from cordify import Harness, Plugin, Inject` bleiben verfügbar. Konkrete
Implementierungen werden aus ihrer Schicht importiert:

```python
from cordify.kernel import Context, Key, plugin
from cordify.adapters.deepseek import DeepSeekLLM
from cordify.plugins.context.compaction import ThresholdCompaction
from cordify.presets import minimal, coding, headless

agent = minimal(DeepSeekLLM(model="deepseek-flash"))
# coding(adapter, root="./repo", shell=True) ergänzt Dateitools und Shell explizit.
```

OTel und zusätzliche Extras für nicht implementierte Fähigkeiten sind nicht
enthalten. MCP, Sandbox-Backends, Checkpoints, Subagents, weitere Modelladapter
und ein Voice-Preset können später in diesen Schichten ergänzt werden.

```sh
python -m pip install -e .
python -m examples.01_tool_agent
```

Ausführbare Beispiele (aus dem Repository-Root):

| Befehl | Zeigt |
| --- | --- |
| `python -m examples.01_tool_agent` | Tool-Loop, Token-Stream, weiterer Turn |
| `python -m examples.02_plugins` | Eigene Services, Konfiguration, Middleware, Cleanup |
| `python -m examples.03_inbox` | Follow-up, Steering und stille Injection mit expliziten Turn-/Step-Projektionen |
| `python -m examples.04_deepseek` | Echter DeepSeek-Adapter, Dateitools und JSONL-Resume |
| `python -m examples.05_bundles_and_injection` | Wiederverwendbare Module, Klassen, DI, Approval, Progress, Nudges |
| `python -m examples.06_explicit_registration` | Plugins und Hooks ohne Dekoratoren |
| `python -m examples.07_context` | Alle fünf Kontextwege: Sektionen, Aufgaben-Kontext, Anhänge, Push und Pull |
| `python -m examples.08_observations` | Browserzustand pro Step: voller aktueller Zustand, kompakte alte Zustände |
| `python -m examples.09_rules` | TestDiscipline als Klasse und deklarative Rules |
| `python -m examples.10_context_strategies` | Spill, Compaction, Observation-Limit und Cache-Metadaten gemeinsam |
| `python -m examples.11_temporal_needs` | Umkehrbare Tools, Prompt-Sektionen, Listener und eigene Effekte bei Service-Wechsel |
| `python -m examples.12_layered_sdk` | Neuer Schichtenaufbau, eigenständiger Kernel und Presets |
| `python -m examples.13_llm_adapters` | Eigener BaseLLM-Adapter mit Retry und struktureller Adapter ohne Vererbung |
| `python -m examples.14_session_log` | Dauerhafte Chunks, reine Log-Projektionen, Request-Replay und unabhängiger Fork |
| `python -m examples.15_tool_output_limits` | 2-MB-Ausgabe begrenzen, ExecuteTool-Waterfall und Weiterlesen mit offset/limit |
| `python -m examples.16_compaction_policies` | Threshold, Turn-Fenster, periodische Compaction, geschützte Turns und Budget-Guard |
| `python -m examples.17_resume_crash` | Prozessneustart mit dauerhaften Freigaben, Inbox und archiviertem JSONL-Tail |
| `python -m examples.18_parallel_tools` | Parallele Calls, stabile Results und Dateikonflikte mit Ressourcen-Schlüsseln |

Nur Beispiel 04 benötigt einen API-Key und das `deepseek`-Extra. Die anderen
Beispiele verwenden deterministische Modellantworten, um das SDK sichtbar zu machen.

```python
import asyncio
from pathlib import Path

from cordify import Bundle, FS, Harness, Inject
from cordify.plugins.agents.loop import agent_loop
from cordify.plugins.tools.fs import fs_local
from cordify.plugins.services import llm
from cordify.testing import ScriptedLLM

workspace = Bundle("workspace")


@workspace.tool
async def count_lines(path: str, fs: Inject[FS]) -> int:
    """Count lines in a workspace file."""
    return len((await fs.read(Path(path))).splitlines())


async def main():
    harness = Harness(
        [
            agent_loop(),
            llm(ScriptedLLM(["Hallo aus Cordify."])),
            fs_local("."),
            workspace,
        ]
    )
    async with harness.session("demo") as agent:
        print((await agent.run("Hallo!")).text)


asyncio.run(main())
```

Unmarkierte Tool-Parameter liefert das Modell; `Inject[T]` löst der Harness bei
jedem Aufruf aus dem Context auf. Inject-Parameter fehlen im Modell-Schema.
Tools mit fehlenden Services sind unsichtbar. Eine Registrierung ruft das Tool
nicht auf; dekorierte Tool- und Hook-Funktionen bleiben direkt testbar.

Unterstützte Formen: Async-Generator mit `@plugin` oder `@plugin(...)`, `Bundle`,
`Plugin`-Klassen mit `@hook`/`@tool`, explizite `Plugin(hooks=..., tools=...)` sowie
`add_hook`/`add_tool`. Jede Montage besitzt einen eigenen Cleanup-Stack.

## Turns, Steps und Inbox

Ein **Turn** bearbeitet eine Nutzeranfrage bis zu ihrem Abschluss. Er enthält
einen oder mehrere **Steps**. Ein Step umfasst genau einen Modellaufruf und die
daraus folgenden Tool-Ausführungen, einschließlich Policy und Ergebnis-Review.
`StepEnd` kommt nach den Tool-Ergebnissen. Braucht das Modell weitere Tools,
beginnt anschließend ein weiterer Step desselben Turns.

Die Inbox ist ein unveränderlicher Snapshot aus dem Session-Log. Sie trennt das
Ziel einer Eingabe von der Frage, ob diese Eingabe Arbeit anfordert:

| API | Ziel | Weckt auf | Während eines laufenden Turns |
| --- | --- | --- | --- |
| `await agent.followup(text)` | Nächster Turn | Ja | Wartet bis zum Abschluss des aktuellen Turns |
| `await agent.steer(text)` | Nächster Step | Ja | Fordert einen weiteren Step zur Kurskorrektur an |
| `await agent.inject(text)` | Nächster Step | Nein | Wartet auf den nächsten ohnehin nötigen Step |

Eine stille Injection verlängert einen ansonsten abgeschlossenen Turn nicht.
Sie bleibt in der Inbox, bis ein Tool-Fortgang, Steering, Follow-up oder ein
Fortsetzungs-Hook den nächsten Step auslöst. Auch im Idle erzeugt `inject()`
keinen Modellaufruf. `steer()` im Idle startet einen Turn, damit die Korrektur
verarbeitet werden kann. Mehrere Follow-ups werden FIFO in separaten Turns
bearbeitet; der nächste wartende Turn kann direkt anschließen.

```python
from cordify import system

await agent.inject(system("Die Tür wurde geöffnet."))  # bleibt im Idle liegen
await agent.followup("Prüfe den Raum.")  # weckt auf, neuer Turn

# Während Modell oder Tools arbeiten:
await agent.steer("Prüfe zuerst die Tür.")  # nächster Step desselben Turns
await agent.followup("Erstelle danach einen Bericht.")  # separater Turn
await agent.when_idle()  # wartet auf den gesamten Lauf
```

Ein bereits laufender Modellaufruf behält seinen Request-Snapshot. Steering
unterbricht auch kein laufendes Tool; die Korrektur gelangt an die nächste
Step-Grenze. `inject()` akzeptiert User- und System-Nachrichten, `steer()` und
`followup()` User-Nachrichten. Strings sind User-Nachrichten.

Für jeden Step wählt `Inbox.for_step(number)` die Eingaben aus. Im ersten Step
kommen höchstens ein Follow-up und die wartenden Step-Eingaben an die Reihe;
weitere Steps erhalten ausschließlich Step-Eingaben. `Inbox.has_wakeup` und
`Inbox.has_step_wakeup` halten Start und Fortsetzung auseinander. Die Zuordnung
von Modus, Ziel und Wakeup wird beim Erzeugen beziehungsweise Laden eines
`InboxAdded` geprüft und beim Resume aus dem Log rekonstruiert.

`PreStep` läuft vor dem Modellaufruf. Dort injizierter Push-Kontext erreicht noch
den vorbereiteten Request. Lehnt ein Hook mit `StepDecision.reject()` ab, bleibt
die Eingabe erhalten: Es gibt einen abgeschlossenen Turn mit Grund `rejected`,
aber keinen Modell-Step. Ein neuer Wakeup kann einen weiteren Versuch auslösen.

Turns und Steps sind selbst `frozen` Projektionen des Logs, keine zusätzliche
veränderliche Historie:

```python
from cordify import derive_turns

assert agent.session.turns == derive_turns(agent.session.events)
for turn in agent.session.turns:
    print(turn.id, turn.closed, turn.reason)
    for step in turn.steps:
        print(step.number, step.request, step.closed, step.reason)
```

Step-Nummern beginnen in jedem Turn bei eins. Während des Laufs können Turn und
Step noch offen sein (`closed=False`, `reason=None`); ein vor dem Request
abgebrochener Step hat `request=None`. Abbruch, regulärer Abschluss und
`max_steps` bleiben über ihre Events und Abschlussgründe sichtbar. Replay und
Fork liefern dieselben Turn-/Step-Projektionen. Das ausführbare
[`03_inbox.py`](examples/03_inbox.py) zeigt zwei Turns mit insgesamt drei Steps.

## Kontext: fünf Wege

| Inhalt | Mechanismus | Lebensdauer und Platz |
| --- | --- | --- |
| Rolle und feste Regeln | `section(stable=True)` | Statischer System-Prefix |
| Tool-Doku | Signatur und Docstring bei `.tool` | Sichtbare Tool-Schemas im Request |
| Änderbares Projektwissen | `section(stable=False)` | System-Prefix hinter stabilen Sektionen |
| Aufgabe und Anhänge | `run(prompt, attachments=...)` | User-Nachricht im Log und in späterer Historie |
| Kontext nur für diese Aufgabe | `run(..., context=...)` | Aktueller Turn; danach nur noch im Log |
| Hinweis auf ein Ergebnis | `result.with_note(...)` | Tool-Ergebnis im Log und in der Historie |
| Stiller Zusatzkontext | `inject(system(...))` | Nächster Step; weckt nicht auf |
| Kurskorrektur | `steer(user(...))` | Nächster Step; weckt auf |
| Browser- oder Screenzustand | `observe(Observation(...))` | Aktuell voll im Tail, frühere Zustände kurz |
| Großes Wissen und Memory | Pull-Tools | Ergebnis nur bei tatsächlichem Abruf |

**1. System-Sektionen** für dauerhafte Regeln und dynamischen Projektkontext:

```python
from pathlib import Path
from cordify import FS, Inject, Plugin, SystemPrompt


class ProjectInstructions(Plugin):
    async def setup(self, prompt: Inject[SystemPrompt], fs: Inject[FS]):
        prompt.section("persona", "You are a code reviewer.", order=0)
        prompt.section(
            "agents_md",
            lambda: fs.read(Path("AGENTS.md")),
            order=50,
            stable=False,
        )
```

Stabile Sektionen kommen vor dynamischen Sektionen, innerhalb jeder Gruppe zählt
`order`. Stabile Callable-Provider werden einmal pro Registrierung ausgewertet;
Provider mit `stable=False` werden vor jedem Request neu ausgewertet. So bleibt
der stabile Prompt-Präfix erhalten. Ein bestimmtes Cache-Verhalten eines
Modellanbieters wird dadurch nicht garantiert. Plugin-Unload entfernt die Sektionen.

**2. Kontext pro Aufgabe** und Anhänge:

```python
from cordify import File, Image

result = await agent.run(
    "Review this PR.",
    attachments=[File("PR.diff"), Image("screenshot.png")],
    context={"ticket": "Fix the authentication regression."},
)
```

`context` erzeugt eigene System-Blöcke vor der Aufgabe. Sie gelten für alle Steps
dieses Turns und verschwinden aus späteren Modell-Requests. Die Blöcke bleiben im
Log. `File` liest Text, `Image` erzeugt ein Bild-Part aus einer lokalen Datei oder
einer URL. Dateiinhalte werden beim Enqueue festgehalten; Anhänge bleiben als Teil
der ursprünglichen User-Nachricht in der Historie. Bildverständnis hängt vom
gewählten Modell ab. `followup` und `stream` akzeptieren dieselben Optionen.

**3. Während des Laufs** stille Nachrichten oder Steering:

```python
from cordify import system, user

await agent.inject(system("utils.py was modified externally."))
await agent.steer(user("Focus on the auth module first."))
```

Strings werden weiterhin als User-Nachrichten angenommen. Explizite `system()`-
Nachrichten ergänzen den Kontext und ersetzen keine registrierten Prompt-Sektionen.
Diese Nachrichten bleiben in Log und Modell-Historie erhalten.

**4. Push-Kontext vor einem Step**, zum Beispiel Memory:

```python
from cordify import Agent, Inject, Plugin, PreStep, hook, system


class RelevantMemories(Plugin):
    @hook(PreStep)
    async def recall(self, ev, next_, memory: Inject[Memory], agent: Inject[Agent]):
        if ev.is_turn_start:
            hits = await memory.search(ev.latest_user_text, k=3)
            if hits:
                await agent.inject(system("Relevant memories:\n" + "\n".join(hits)))
        return await next_()
```

`Memory` ist dabei ein eigener Service. Injections aus einem `PreStep`-Hook werden
noch in **diesem** Step aufgenommen. Externe Injections nach dessen Aufnahmegrenze
warten auf den nächsten Step. `ev.is_turn_start` und `ev.latest_user_text` erlauben
gezielte Retrievals ohne eigene Turn-Zähler.

**5. Pull-Kontext durch ein Tool** nutzt dieselbe DI:

```python
memory_tools = Bundle("memory")


@memory_tools.tool
async def recall(query: str, memory: Inject[Memory]) -> list[str]:
    """Search long-term memory for relevant facts."""
    return await memory.search(query, k=5)
```

Push eignet sich für Regeln und kleinen Kontext, der immer sichtbar sein soll;
Pull für große Wissensbestände, die das Modell bei Bedarf abfragt.
Eine ausführbare Implementierung einschließlich `Memory` steht in Beispiel 07.

## Observations für Browser und andere Live-Zustände

```python
from cordify import Agent, Inject, Observation, Plugin, PreStep, hook


class BrowserObserver(Plugin):
    @hook(PreStep)
    async def observe(self, ev, next_, browser: Inject[Browser], agent: Inject[Agent]):
        state = await browser.snapshot()
        agent.observe(
            Observation(
                source="browser",
                full=state.render(),
                summary=f"[page state: {state.url} — {state.title}]",
            )
        )
        return await next_()
```

`observe()` registriert synchron einen Zustand für den nächsten zugelassenen Step
und weckt den Agenten nicht. Mehrere ausstehende Zustände derselben `source`
werden auf den neuesten zusammengeführt. Im aktuellen Step erscheint `full`, in
späteren Steps `summary`. Das Log behält beide Werte; `ModelRequested` speichert
den jeweils tatsächlich verwendeten Request. Replay und Fork brauchen keinen
erneuten Browserzugriff. Das ist eine eigene Projektion, keine Änderung alter Events.
`Browser` ist ein eigener Service; Beispiel 08 enthält einen ausführbaren
Browser-Dummy, den Observer sowie `click`- und `snapshot`-Tools.

## Plugins und Injection

### Umkehrbare Effekte

Jede aktive Registrierung gehört zum Cleanup-Stack ihres Contexts. Sie bekommt
einen asynchronen, idempotenten **Disposer**: `await remove()` entfernt genau
diese Registrierung. Beim Plugin-Teardown wird derselbe Disposer automatisch
aufgerufen. Manuelle Entfernung und späterer Teardown funktionieren gemeinsam.

| Direkte Registrierung | Rückgabe |
| --- | --- |
| `ctx.provide(Service, implementation)` | Disposer für den Service |
| `ctx.add_listener(Event, callback)` | Disposer für den Listener |
| `registry.register(callback, name="read")` | Disposer für das Tool |
| `prompt.section("project", content)` | Disposer für die Prompt-Sektion |
| `ctx.effect(cleanup)` | Disposer für einen eigenen synchronen oder asynchronen Effekt |
| `agent.add_tool(callback)` / `agent.add_hook(Event, callback)` | Disposer für die laufende Session |

Die Dekoratoren `@ctx.on(...)`, `@registry.tool` und `@agent.tool` geben weiterhin
die ursprüngliche Funktion zurück. Ihre Registrierungen werden genauso
automatisch entsorgt. `Harness` und `Bundle` sammeln zunächst Rezepte; die
Effekte entstehen erst bei der Montage in einem Context.
Auch über `agent.on` und `agent.add_hook` angelegte Listener gehören zum aktuell
aktiven Plugin-Context; außerhalb eines Plugins gehören sie zur Session.

```python
from cordify import Context, Emit, SystemPrompt, ToolRegistry


class Changed(Emit):
    pass


async def read() -> str:
    return "temporary tool"


async with Context() as ctx:
    registry = ToolRegistry(ctx)
    prompt = SystemPrompt(ctx)
    remove_tool = registry.register(read)
    remove_listener = ctx.add_listener(Changed, lambda event: print("changed"))
    remove_section = prompt.section("project", "Use this workspace.")

    await remove_tool()
    await remove_tool()  # idempotent
    await remove_listener()
    # remove_section wird beim Verlassen des Contexts automatisch ausgeführt.
```

Eigene Ressourcen werden mit `ctx.effect(resource.aclose)` oder einem eigenen
Cleanup-Callback angehängt, sobald sie angelegt wurden. So greift der Rollback
auch bei einem teilweise fehlgeschlagenen Setup. Cleanup läuft in umgekehrter
Registrierungsreihenfolge und führt die übrigen Disposer auch nach einem Fehler
aus. Ein Fehler beim Registrieren nach erfolgreichem Klassen-Setup ruft zusätzlich
`Plugin.teardown()` auf.

Tools, Listener und Prompt-Sektionen werden erst nach erfolgreichem Plugin-Setup
für andere Aufrufer sichtbar. Beim Beginn des Teardowns verschwinden sie aus der
Auswahl, auch wenn asynchrones Cleanup noch wartet. Überschriebene Tools und
Prompt-Sektionen geben dabei die vorherige Registrierung wieder frei.
Bereits entfernte Listener werden in einem laufenden Dispatch übersprungen;
eine während des Renderns entfernte Sektion liefert keinen veralteten Prompt-Text.

### needs und temporale Komposition

`needs` beschreibt **wann ein Plugin lebt**. Das gilt während der gesamten
Montage, nicht nur beim Start. `Inject[T]` beschreibt, **woher ein Parameter
kommt**. Die explizite needs-Liste ist die klare Form für den Lebenszyklus:

```python
@plugin(needs=[Browser, ToolRegistry])
async def browser_tools(ctx: Context):
    browser = ctx[Browser]

    @ctx[ToolRegistry].tool
    async def title() -> str:
        """Read the current page title."""
        return await browser.title()

    yield
```

Fehlt `Browser`, bleibt die Montage `waiting`. Wird er bereitgestellt, beginnt
Setup; verschwindet er, werden die abhängigen Plugins vor ihrem Provider
abgebaut. Tools, Hooks, Services, Prompt-Sektionen und Effekte dieser Montage
verschwinden. Kehrt der Service zurück, wird derselbe Plugin-Beleg mit einem
neuen Context, einem neuen Async-Generator beziehungsweise einer frischen
Klasseninstanz wieder aktiviert. Der eingefangene `browser` oben ist deshalb
immer an die aktuelle Aktivierung gebunden. Auch Service-Overrides und das
Zurückfallen auf die vorherige Implementierung führen zum Neuaufbau.

```python
feature = await ctx.plugin(browser_tools())  # wartet, falls Browser fehlt
remove = ctx.provide(Browser, local_browser)
await ctx.settle()  # feature.status == "active"

await remove()
await ctx.settle()  # feature.status == "waiting"

ctx.provide(Browser, replacement_browser)
await ctx.settle()  # frischer Aufbau
await feature.aclose()  # permanent "disposed"
```

Die Komposition läuft automatisch auf dem Event-Loop. `settle()` ist die
explizite Barriere für Tests und Anwendungen, die den fertigen Übergang benötigen.
Ein unvollständiges Setup wird bei Dependency-Verlust oder Shutdown abgebrochen
und zurückgerollt. Services eines Providers werden für andere Plugins erst nach
erfolgreichem Setup sichtbar. `handle.error` zeigt Setup-Fehler; `settle()` meldet
Fehler automatischer Aktivierungen als ExceptionGroup. Fehlgeschlagene Setups
werden bei unveränderter Dependency nicht endlos wiederholt. Dynamische Zyklen
werden gemeldet.

Ein expliziter Hot-Reload benutzt dieselben Lebenszeiten: Den alten Provider mit
`await provider.aclose()` entladen und seine neue Version mit
`await ctx.plugin(new_provider_spec)` montieren. Die Rezepte seiner abhängigen
Plugins bleiben erhalten; nach `await ctx.settle()` sind sie mit frischem Zustand
und genau einer neuen Registrierung pro Tool, Sektion und Listener aufgebaut.
[`11_temporal_needs.py`](examples/11_temporal_needs.py) zeigt Service-Verlust und
Neuaufbau für alle drei Registrierungsarten samt eigenem Cleanup-Effekt.

Die Formen sind gleichwertig:

```python
@plugin(needs=[Browser])
async def feature(ctx):
    yield


class BrowserFeature(Plugin):
    needs = (Browser,)


bundle = Bundle("browser-feature", needs=[Browser])
explicit = Plugin(name="browser-feature", needs=[Browser], hooks=[], tools=[])
```

`Context.install()` behält auch momentan nicht erfüllte Plugins als wartende
Rezepte. `strict=True` verlangt stattdessen erfüllbare Start-Abhängigkeiten und
rollt einen unvollständigen Installationssatz zurück. Kindkontexte haben eigene
Service-Sichten; Änderungen in einem isolierten Geschwisterkontext bauen die
anderen Plugins nicht um. `Agent` wartet vor jedem Step auf die Komposition.

Inject-Parameter in Plugin-Setup und Klassen-Hooks ergänzen `needs` weiterhin als
Kurzform. Unmarkierte Service-Typen lösen keine automatische Injection aus.
Tool-Injection allein macht ein Tool bei fehlendem Service unsichtbar; für die
Lebensdauer des ganzen Features wird `needs` ausdrücklich angegeben.

```python
from cordify import Context, Inject, SystemPrompt, ToolRegistry, plugin


@plugin
async def project_rules(ctx: Context, prompt: Inject[SystemPrompt]):
    prompt.section("project", "Use Python 3.12+.", order=10)
    yield


# Auch @plugin(), @plugin(name="...") und @plugin(needs=[...]) werden akzeptiert.
# Inject-Parameter ergänzen needs automatisch.
```

`Inject[T]` ist ein PEP-695-Alias für `Annotated[T, InjectMarker()]` und wird von
Typprüfern als `T` behandelt. Normale Parameter eines Plugins sind Konfiguration;
normale Tool-Parameter kommen vom Modell. Bei Hooks bleiben Event und `next_`
normale Parameter. Der Agent wird pro Session injiziert:

```python
from cordify import Agent, Bundle, Inject, ReviewToolResult

safety = Bundle("safety")


@safety.on(ReviewToolResult)
async def nudge(event, next_, agent: Inject[Agent]):
    result = await next_()
    if result.is_error:
        await agent.inject("Read the error before retrying.")
    return result
```

`Bundle` kennt beim Definieren keinen Agenten. Die Montage in mehreren Sessions
erzeugt jeweils eigene Registrierungen. Eine `Plugin`-Instanz ist eine Vorlage:
pro Montage wird `clone()` aufgerufen (standardmäßig `deepcopy`). Zustand gehört
dadurch der Session. Für nicht kopierbare Konfigurationen kann `clone()` eine neue
Instanz erzeugen; Ressourcen werden in `setup()` geöffnet und in `teardown()` oder
über `ctx.effect()` geschlossen. Bei Funktionsplugins gehört lokaler Zustand in
den Async-Generator, bei Bundles in einen Service oder ein Klassenplugin.

Tools unterstützen `@bundle.tool`, `@bundle.tool()` und
`@bundle.tool(name="name", timeout=30)`. Das gilt auch für `@tool`, `@agent.tool`,
`@harness.tool` und `@ctx[ToolRegistry].tool`. Für Events ist die Event-Klasse bei
`on(Event)`/`hook(Event)` Pflicht. `ctx.on(Event, fn)`, `bundle.add_hook(Event, fn)`,
`harness.add_hook(Event, fn)` und `agent.add_hook(Event, fn)` sind die imperativen
Varianten; `add_tool(fn, ...)` registriert Tools ohne Dekorator.

Für call-lokale Daten steht `call: Inject[ToolCallContext]` bereit.
`await call.progress(text)` schreibt einen dauerhaften `ToolProgress`-Event.
Async-Tools werden durch normale asyncio-Cancellation abgebrochen.
`call.signal` ist ein `asyncio.Event`, das der Harness bei Tool-Cancellation setzt.

| API | Grenze | Weckt den Agenten? |
| --- | --- | --- |
| `await agent.followup(text)` | Nächster eigener Turn | Ja |
| `await agent.steer(text)` | Nächster Step | Ja |
| `await agent.inject(text)` | Nächster Step | Nein |

`when_idle()` wartet auf den gesamten Agenten einschließlich weiterer Turns.
`run()` und `stream()` beobachten diesen Zeitraum, keinen einzelnen Enqueue-Beleg.
Die Rückgabe von `followup`, `steer` und `inject` ist die Message-ID.

Die Tool-Pipeline hat feste Phasen: **ToolPolicy → ExecuteTool → ReviewToolResult**.
Abgelehnte Calls überspringen Ausführung und Review; ihr Ergebnis wird trotzdem
als `ToolReturned` geloggt. `Approver` ist ein austauschbarer Service.

`callback_approver(fn)` stellt einen Callback als `Approver` bereit;
`approval_gate(risky={...})` fragt ihn. Entscheidungen sind `Decision.ALLOW`,
`DENY` und `ALWAYS` (Freigabe für Toolname und Policy-Scope innerhalb derselben
Session-ID, auch nach Resume, bis zum Widerruf).
Ein `ToolPolicy`-Hook kann `Decision.ASK` zurückgeben, um den Approver aufzurufen.
Die kompakte Form `approval_gate(risky={...}, ask=fn)` ist ebenfalls möglich.
`ToolResult.with_note(text)` reichert das Modell-Ergebnis an;
`await agent.inject(text)` fügt Kontext hinzu und `await agent.steer(text)` setzt
eine neue Step-Eingabe. `ToolReturned` beobachtet das bereits festgeschriebene Ergebnis.

`Harness` montiert pro Session frische Contexts, ToolRegistry und SystemPrompt
sowie Standard-Services für TokenCounter, Compactor und SpillStore.
Session-Events und Modellwerte sind unveränderlich; Inbox und Modell-Historie
werden aus dem Log abgeleitet. `session_jsonl()` speichert JSONL mit einer
Versionsnummer und exklusivem OS-Schreiblock. `ModelRequested` enthält den
tatsächlichen Request. Auch `AssistantChunk` ist ein dauerhaftes Session-Event:
Chunks werden vor ihrer Live-Ausgabe gespeichert. Beim Resume werden
unvollständige Antworten abgeschlossen, unterbrochene Turns geschlossen und
fehlende Tool-Ergebnisse ergänzt.

`PreStep` ist ein Waterfall über die an dieser Grenze angebotenen User-Messages.
`StepDecision(messages)` übernimmt sie; `StepDecision.reject()` lässt sie in der
Inbox. Ersetzte Messages behalten ihre ID, wenn ihre Inbox-Identität erhalten
bleiben soll. Eingaben nach dieser Grenze erreichen frühestens den nächsten Step.
`Session.fork(new_id, at=offset)` kopiert einen Log-Präfix bis zu einem geschlossenen
Turn; `at` ist eine exklusive Event-Position. Ein Fork hat zunächst keinen Writer.

Der generische Kern bietet `Emit` (seriell), `Bail[R]` (erster Nicht-None-Wert),
`Waterfall[R]` (Middleware mit einmaligem `await next_()`) und `Parallel`
(TaskGroup). Services, Listener, Tools und Prompt-Sektionen verschwinden beim
Plugin-Unload; frühere überschattete Registrierungen werden wieder sichtbar.
`ctx.extend()` erzeugt einen Kindkontext mit eigenen Service- und Event-
Registrierungen. Pro Harness-Session gibt es einen unabhängigen Root-Context.

## Begrenzte Tool-Ausgaben

Der Standard-Agent begrenzt jedes Tool-Ergebnis auf **32.000 Unicode-Zeichen**,
einschließlich Kürzungshinweis. Die Grenze greift vor `ToolReturned`, Session-Log
und dem nächsten Modell-Request. Auch Fehlertexte und Erweiterungen durch
Review-Hooks werden begrenzt. Das ist eine feste Policy im Agent, die ohne
zusätzliches Plugin aktiv ist.

```python
from cordify import ToolOutputLimit
from cordify.presets import coding

harness = coding(model, root="./repo", tool_output_limit=ToolOutputLimit(max_chars=8000))
async with harness.session("review", tool_output_limit=ToolOutputLimit(max_chars=4000)) as agent:
    result = await agent.run("Inspect the project.")
```

`Harness`, `minimal`, `coding` und `headless` akzeptieren `tool_output_limit`;
eine Session kann die Vorgabe überschreiben. `max_chars` muss mindestens 256
sein, damit der Hinweis Platz hat. Das Budget ist eine Zeichengrenze pro Ergebnis,
kein anbieterspezifisches Token-Limit oder Gesamtbudget des Requests.

Die Kürzung behält etwa drei Viertel des verfügbaren Inhalts vom Anfang und ein
Viertel vom Ende, damit beispielsweise ein Fehler am Schluss sichtbar bleibt.
Der Mittelteil wird durch einen Hinweis ersetzt:

```text
[Output truncated; middle omitted (2097168 characters total). Use offset/limit for smaller ranges or filter the command output.]
```

Status, Exit-Code und Spill-Referenz bleiben erhalten.
`ToolResult.truncation` enthält `original_chars`, `retained_chars` und `max_chars`;
das Log und seine JSON-Codecs erhalten diese Metadaten. `retained_chars` zählt
den übernommenen Originaltext, während `max_chars` den Hinweis einschließt.
Kleine Ergebnisse bleiben unverändert. Die Policy ist idempotent und kann auch
als eigener Waterfall um `ExecuteTool` benutzt werden:

```python
from cordify import Bundle, ExecuteTool, ToolOutputLimit

limits = Bundle("output-limits")
budget = ToolOutputLimit(max_chars=8000)


@limits.on(ExecuteTool)
async def truncate(event, next_):
    return budget.apply(await next_())
```

Dieser Waterfall kürzt bereits vor dem Ergebnis-Review. Die verbindliche
Agent-Grenze greift zusätzlich **nach** `ReviewToolResult`, damit nachträglich
ergänzte Texte sie nicht umgehen. `SpillLargeResults` braucht die vollständige
Ausgabe für seine Datei; bei Spill verwendet man die finale Agent-Grenze ohne
die zusätzliche frühe Kürzung. So bleibt das vollständige Ergebnis abrufbar.

Das eingebaute Dateitool unterstützt echte Bereiche:

```python
read_file(path="large.log", offset=0, limit=8000)
read_file(path="large.log", offset=8000, limit=8000)
```

`offset` ist eine nullbasierte Zeichenposition, `limit` liegt zwischen 1 und
16.000 und beträgt standardmäßig 8.000. Weitere Daten werden mit einem Hinweis
auf den nächsten Offset angezeigt. Das funktioniert auch bei mehrbyteiligen
Unicode-Zeichen. Für Shell-Ausgaben filtert man den Befehl oder schreibt in eine
Datei und liest anschließend Bereiche. Die Adapter können intern größere Daten
lesen; die Policy begrenzt das Ergebnis, das ins Log und ans Modell gelangt.

[`15_tool_output_limits.py`](examples/15_tool_output_limits.py) zeigt den
Waterfall mit einer 2-MB-Ausgabe, erhaltenem Exit-Code und anschließenden
Bereichsreads vollständig offline.

## Parallele Tool-Calls und Konflikte

Ein Modell kann mehrere Calls in einem Step zurückgeben. Der Harness führt
explizit freigegebene Tools gleichzeitig aus, mit `max_parallel_tools=4` als
Standard. Ohne Freigabe bildet ein Tool eine Barriere: Vorherige Calls werden
einschließlich Review abgeschlossen, dann läuft dieses Tool allein.

| Registrierung | Verhalten innerhalb eines Agenten-Batches |
| --- | --- |
| `@tools.tool` | Seriell; Barriere vor und nach dem Call |
| `@tools.tool(parallel=True)` | Darf mit anderen freigegebenen Calls gleichzeitig laufen |
| `@tools.tool(parallel=True, resources=("database:users",))` | Derselbe Schlüssel serialisiert konkurrierende Calls in Modellreihenfolge |
| `resources=lambda call: (...)` | Synchrone, nebenwirkungsfreie Auflösung exklusiver Ressourcen aus dem Call |
| `max_parallel_tools=1` | Erzwingt insgesamt serielle Ausführung |

Die Optionen funktionieren bei Bundle, Klassen-`@tool`, Harness, Agent und
`ToolRegistry.register`. Sie sind SDK-Metadaten und erscheinen nicht als
Modell-Argumente im Tool-Schema.

```python
from cordify import Bundle, Inject, ToolCall
from cordify.presets import minimal

tools = Bundle("records")


def record_resources(call: ToolCall) -> tuple[str, ...]:
    return (f"record:{call.arguments['record_id']}",)


@tools.tool(parallel=True, resources=record_resources)
async def update_record(record_id: str, text: str, store: Inject[RecordStore]) -> str:
    """Update one record."""
    await store.write(record_id, text)
    return "saved"


# RecordStore ist ein eigenes Protocol; seine Implementierung als Service montieren.
harness = minimal(model, plugins=[store_provider, tools], max_parallel_tools=4)
# Pro Session überschreibbar:
# async with harness.session("serial", max_parallel_tools=1) as agent: ...
```

Verschiedene Schlüssel dürfen überlappen. Ein Call kann mehrere Schlüssel
belegen; ihre Reihenfolge spielt keine Rolle. Konflikte werden vor der
Ausführung als Abhängigkeiten geplant, sodass kein wechselseitiges Warten
beim Erwerb mehrerer Sperren entsteht. Nachfolger warten bis zum Abschluss
des Vorgängers einschließlich Review.

Im Coding-Preset ist `read_file` parallel freigegeben. `write_file` und
`run_command` bleiben Barrieren. Zwei Writes auf dieselbe Datei laufen dadurch
in Modellreihenfolge; auch `a.txt` und `./a.txt` können nicht überlappen.
Eigene parallele Datei-Tools müssen kanonische Pfade als gemeinsame
Ressourcen-Schlüssel verwenden, für Reads und Writes gleichermaßen.
Die Koordination gilt für den jeweiligen Agenten-Batch. Für Zugriffe anderer
Agenten oder Prozesse braucht das FS-/Datenbank-Backend eigene Sperren.

Innerhalb einer parallelen Gruppe laufen Policies und Approval-Fragen zuerst
in Modellreihenfolge. Erst danach beginnt die parallele `ExecuteTool`-Phase.
`ReviewToolResult`, zustandsbehaftete Rules und `ToolReturned` folgen wieder
der Modellreihenfolge. Auch die Tool-Nachrichten im nächsten Modell-Request
halten diese Reihenfolge ein. `ToolProgress` und Ausführungsereignisse zeigen
den tatsächlichen zeitlichen Ablauf; das gesamte Log wird nicht nachträglich
umsortiert. Eigene `ExecuteTool`-Hooks müssen mit der freigegebenen Parallelität
zurechtkommen.

Tool-Fehler, Timeouts und Ablehnungen ergeben einzelne Tool-Results und brechen
unabhängige Calls nicht ab. Ein Agent-Abbruch beendet und wartet auf alle
laufenden Tasks. Die feste Output-Grenze greift nach jedem Review.
Ein abschließend geprüftes, begrenztes Ergebnis wird zuerst als `ToolCompleted`
gespeichert und danach als `ToolReturned` veröffentlicht. Bei Resume übernimmt
die Reparatur gespeicherte Completions in die geordnete Result-Folge, ohne
Tools erneut auszuführen. Fehlt diese Completion, bleibt ein bereits gestarteter
Call nach einem Crash mit unbekanntem externem Ergebnis markiert.

[`18_parallel_tools.py`](examples/18_parallel_tools.py) startet drei Calls
gleichzeitig, lässt sie in umgekehrter Reihenfolge fertig werden und prüft die
stabile Modell-Historie. Ein zweiter Durchlauf zeigt kanonische Dateischlüssel
für zwei Writes auf dieselbe Datei neben einem unabhängigen Write.

## Das Session-Log ist die Quelle der Wahrheit

Das Log enthält ausschließlich registrierte `frozen` Dataclasses wie `TurnStart`,
`StepStart`, `AssistantChunk`, `AssistantMessage`, `ToolCalled`,
`ToolCompleted` und `ToolReturned`.
Session, Codec und Seed-Import prüfen auch die verschachtelten Werte auf
Unveränderlichkeit. `session.events` liefert einen unveränderlichen Tupel-Snapshot;
`append()` fügt Ereignisse hinzu. Compaction, Inbox-Entnahme und die Reparatur
unterbrochener Läufe erzeugen weitere Events und verändern keine alten Einträge.

Die öffentlichen Projektionen sind reine Funktionen:

```python
from cordify import Session, derive_messages, derive_requests

history = derive_messages(session.events)  # identisch zu session.messages
requests = derive_requests(session.events)  # identisch zu session.requests
replay = Session("replayed", seed=session.events)
assert replay.messages == history
assert replay.requests == requests
```

`derive_messages()` baut die Gesprächshistorie aus dem Log auf: System-Prompt,
User- und Assistant-Nachrichten, Tool-Ergebnisse, Kontext, Observations und
Compaction. Ein offener Stream erscheint als eine partielle Assistant-Nachricht.
Seine `AssistantChunk`s tragen eine gemeinsame `message_id`; der abschließende
`AssistantMessage` verwendet dieselbe ID. Die Projektion führt beides genau einmal
zusammen und erzeugt dabei keine neuen zufälligen IDs.

`derive_requests()` liefert die tatsächlich gesendeten `ModelRequest`-Snapshots,
einschließlich Tool-Schemas, Token-Limit, Cache-Breakpoints und Änderungen durch
Request-Hooks. Dadurch lässt sich die Modell-Historie **für jeden damaligen Step**
exakt aus dem Log lesen, auch wenn sich Services oder Plugins inzwischen geändert
haben. Die aktuelle Gesprächsprojektion und frühere Request-Snapshots bleiben
getrennt abrufbar.

```python
for request in replay.requests:
    print(request.model, request.messages, request.tools)

fork = replay.fork("alternative")  # unabhängiges Log mit demselben geschlossenen Präfix
```

Replay und Fork benötigen weder den damaligen Provider noch Tool-Ausführung oder
Plugin-Setup. Evals und Debugging können dieselben Events und Request-Snapshots
auswerten. Die Codecs `encode_event()` und `decode_event()` in
`cordify.core.session` erhalten die Projektionen bei einem JSON-Roundtrip.

Bei einem Crash nach gespeicherten Chunks ergänzt `repair_interrupted()` ein
`AssistantMessage(interrupted=True)` mit genau diesem Text und Reasoning.
Erneutes Reparieren ändert das Log nicht mehr. Ein fehlgeschlagener Schreibversuch
publiziert keinen Chunk und übernimmt dessen Text auch nicht in eine spätere
Assistant-Nachricht. Bereits vor der Erweiterung gespeicherte Logs ohne Chunks
werden weiterhin über ihre `AssistantMessage`-Events projiziert.

[`14_session_log.py`](examples/14_session_log.py) zeigt Live-Stream,
Serialisierung, Replay, historische Modell-Requests und einen Fork vollständig
offline. Tests rekonstruieren jeden Event-Präfix vor und nach Serialisierung und
prüfen identische Nachrichten und Requests.

## Resume nach einem Prozessneustart

`session_jsonl()` lädt beim Öffnen derselben Session-ID den gespeicherten
Zustand. Die Anwendung montiert ihre Services und Plugins neu; Nachrichten,
Inbox, Freigaben, Compaction und weitere protokollierte Zustände werden aus dem
Log rekonstruiert.

```python
from cordify.plugins.permissions.approval import approval_gate
from cordify.plugins.persistence import session_jsonl
from cordify.presets import coding

harness = coding(
    model,
    root="./repo",
    shell=True,
    plugins=[
        approval_gate(risky={"run_command"}, ask=ask_user, scope="workspace-shell"),
        session_jsonl(".sessions", fsync=True, recover_tail=True),
    ],
)
async with harness.session("review-42") as agent:
    await agent.when_idle()  # verarbeitet gespeicherte Eingaben mit Wakeup
    result = await agent.run("Setze die bisherige Arbeit fort.")
```

`fsync=True` flusht jeden gespeicherten Record zusätzlich auf den Datenträger.
Der exklusive OS-Schreiblock verhindert zwei gleichzeitige Writer für dieselbe
Session und wird auch beim Prozessabbruch vom Betriebssystem freigegeben.

### Dauerhafte Inbox und Freigaben

| Zustand | Dauerhafte Darstellung und Resume |
| --- | --- |
| Follow-up und Steering | `InboxAdded` enthält Nachricht, Ziel und Wakeup; unbearbeitete Eingaben wecken den neuen Agenten |
| Stille Injection | Bleibt gespeichert und wartet auch nach Resume auf den nächsten Step |
| Approval-Frage | `ApprovalRequested` enthält Call, Argumente, Grund, Request-ID und Policy-Scope |
| Antwort | `ApprovalResolved` hält `ALLOW`, `DENY` oder `ALWAYS` vor Beginn der Ausführung fest |
| Wiederverwendung | Neue Approval-Events verweisen auf die ursprüngliche `ALWAYS`-Freigabe |
| Widerruf | `ApprovalRevoked` entfernt die Freigabe aus der aktiven Log-Projektion |
| Ausführungsgrenze | `ToolPolicyApplied` protokolliert die Entscheidung; `ToolExecutionStarted` wird vor der Ausführung gespeichert |
| Fertige Tools | `ToolCompleted` hält das geprüfte Ergebnis fest; Resume ergänzt fehlende `ToolReturned`-Events in Call-Reihenfolge |

`agent.session.approvals` ist ein unveränderlicher Snapshot mit `records`,
`pending` und `grants`. Eine einzelne `ALLOW`-Antwort gilt für den zugehörigen
Call. Eine `ALWAYS`-Freigabe gilt für Toolname und Scope derselben Session-ID.
Unterschiedliche Policies können mit `approval_gate(..., scope="...")`
getrennte Freigaben verwenden. Auch `ToolPolicy` mit `Decision.ASK` läuft durch
denselben dauerhaften Approval-Pfad.

```python
grant = agent.session.approvals.grant_for("run_command", scope="workspace-shell")
if grant is not None:
    await agent.revoke_approval(grant.request_id)
```

Wiederholter Widerruf ist idempotent. Forks bekommen standardmäßig Widerrufe für
die aktiven Freigaben ihres kopierten Präfixes. Mit
`session.fork("trusted-copy", inherit_approvals=True)` kann die Anwendung die
Übernahme ausdrücklich auswählen.

### Unterbrochene Arbeit

Vor dem Start des neuen Drivers ergänzt `repair_interrupted()` das Log:

- Gespeicherte Text- und Reasoning-Chunks werden zu einer unterbrochenen
  Assistant-Nachricht abgeschlossen.
- Unbeantwortete Approval-Fragen erhalten den Status `interrupted` und erteilen
  keine Freigabe. Ein neuer Versuch benötigt eine neue Entscheidung.
- Tool-Calls ohne Ergebnis erhalten ein unterbrochenes Fehler-Ergebnis.
  Für bereits gestartete Ausführungen wird der externe Ausgang als unbekannt
  markiert; diese Calls werden beim Resume nicht automatisch wiederholt.
- Offene Steps und Turns werden geschlossen. Weitere Reparaturaufrufe erzeugen
  keine doppelten Abschlüsse.

Der Agent verarbeitet anschließend gespeicherte Inbox-Eingaben gemäß ihrem
Wakeup. Ohne solche Eingaben wartet er auf den nächsten `run()`, `followup()`
oder `steer()`. Ein geplanter Stopp kann mit
`await agent.cancel(keep_inbox=True)` die ausstehende Inbox erhalten.

### Abgerissener letzter Schreibvorgang

Standardmäßig weist der JSONL-Adapter unvollständige Records zurück.
`recover_tail=True` erlaubt eine gezielte Reparatur des letzten Records ohne
abschließende Newline: Seine Bytes werden vor der Reparatur in einer
`*.incomplete-<offset>-<sha256>.bin`-Datei archiviert. Der vollständige Log-Präfix
bleibt bytegleich. Ein `LogTailRecovered` protokolliert Offset, Bytezahl,
Hash und Archivpfad. Auch eine unterbrochene Reparatur kann beim nächsten Laden
abgeschlossen werden.

Vollständige, aber ungültige Records, Sequenzfehler und unbekannte Event-Typen
werden weiterhin abgewiesen. Eine Approval-Antwort ohne vollständigen
Record-Abschluss wird nicht als Freigabe übernommen. Historische Logs ohne
Approval-Events enthalten keine rekonstruierbare `ALWAYS`-Freigabe; die neue
Ausführung fragt entsprechend ihrer Policy erneut.

[`17_resume_crash.py`](examples/17_resume_crash.py) startet einen separaten
Prozess, beendet ihn ohne Teardown und öffnet die Session mit einem frischen
Harness erneut. Die Tests beenden Prozesse zusätzlich während eines laufenden
Tools, während einer offenen Approval-Frage und mit stiller Inbox im Idle.

## Entwicklung und Grenzen

```sh
python -m pip install -e ".[dev]"
python -m pytest
ruff check .
```

JSONL-Append ist synchron und flusht pro Event; `fsync=True` aktiviert zusätzlich
einen Datenträger-Flush. Beschädigte oder unbekannte Records werden abgewiesen.
Das Log-Format ist ein eigenes, versioniertes Cordify-Format. Aktuell wird beim
Projizieren die Historie vollständig gelesen; große Sessions brauchen später
inkrementelle Projektionen. Compaction verkleinert die Modell-Historie, nicht das
append-only Log.

Sync-Tools laufen im Threadpool. Ein Timeout oder Abbruch setzt das
`ToolCallContext.signal`; der Harness wartet auf das Ende des Workers, bevor er
Ressourcen freigibt. Ein Python-Thread lässt sich nicht hart stoppen, daher kann
dieses Warten den konfigurierten Timeout überschreiten. Dasselbe gilt für lokale
Dateioperationen. `fs_local` prüft auf Pfade außerhalb des Workspace, bietet aber
keine OS-Sandbox gegen konkurrierende Symlink-Änderungen. `shell_local` startet
lokale Prozesse; es ist ebenfalls kein Sandbox-Backend.

Für DeepSeek:

```sh
python -m pip install -e ".[deepseek]"
# DEEPSEEK_API_KEY in der Umgebung setzen
python -m examples.04_deepseek
```

Die Modell-ID ist über `deepseek(model=...)`, `Harness(model=...)` oder
`harness.session(..., model=...)` konfigurierbar. Eigene Modelle implementieren
das kleine `LLM.stream(ModelRequest)`-Protocol.

Diese Version enthält keinen MCP-Client,
Realtime-Transport, Docker-Backend oder Subagenten. Compaction und die folgenden
Kontextstrategien werden explizit als Plugins ausgewählt.
Die entsprechenden Erweiterungen können auf denselben Services und Events aufbauen.

## Eigene LLM-Adapter

`LLM` bleibt ein `Protocol`. `BaseLLM` ist ein optionaler Helfer für Retry,
exponentiellen Backoff und das Schließen der Streams pro Versuch. Eigene Provider
implementieren `_stream_once()` und bei Bedarf `is_retryable(error)`:

```python
from collections.abc import AsyncIterator

from cordify.adapters.base import BaseLLM, RetryPolicy
from cordify.seams import ModelChunk, ModelRequest


class DemoLLM(BaseLLM):
    async def _stream_once(self, request: ModelRequest) -> AsyncIterator[ModelChunk]:
        yield ModelChunk(text="Hello from my adapter.")


adapter = DemoLLM(retry=RetryPolicy(max_attempts=3, initial_delay=0.5, max_delay=8))
```

`max_attempts` zählt den ersten Versuch mit; `1` schaltet Retries aus. Die
Standardwerte sind drei Versuche, 0,5 Sekunden Anfangspause, Faktor 2 und eine
Obergrenze von 8 Sekunden. Die Basis wiederholt `ConnectionError` und
`TimeoutError`. DeepSeek ergänzt HTTP 408, 429, 5xx sowie Netzwerk-, Timeout-
und Remote-Protokollfehler von HTTPX. Authentifizierungs- und ungültige
JSON-Antworten werden direkt weitergegeben.

**Ab dem ersten ausgegebenen `ModelChunk` gibt es keinen Retry**, auch bei
Reasoning, Usage oder Tool-Calls. Dadurch entstehen keine doppelten sichtbaren
Ausgaben. Cancellation und explizites Schließen beenden den aktuellen Stream;
Cancellation während der Backoff-Pause startet keinen weiteren Versuch.
Die API braucht keine ABC und keinen Eintrag einer Basisklasse im DI-Container.
`ScriptedLLM` erfüllt das Protocol weiterhin direkt.

`DeepSeekLLM` verwendet den Helfer bereits. Dieselbe Konfiguration kann über
das bestehende Provider-Plugin übergeben werden:

```python
from cordify import Harness
from cordify.adapters.base import RetryPolicy
from cordify.adapters.deepseek import deepseek
from cordify.plugins.agents.loop import agent_loop

harness = Harness(
    [
        deepseek(retry=RetryPolicy(max_attempts=4, initial_delay=1, max_delay=10)),
        agent_loop(),
    ]
)
```

Für SSE-basierte Provider verarbeitet
`cordify.adapters.sse.iter_sse_data(response.aiter_lines())` gemeinsame
Data-Frames inklusive mehrzeiliger Daten und Kommentaren. Die Interpretation
von JSON-Deltas, Tool-Call-Fragmenten und `[DONE]` bleibt beim Provider.
Das Framing folgt den
[SSE-Regeln des HTML-Standards](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation).
Das vollständig offline ausführbare Beispiel
[`13_llm_adapters.py`](examples/13_llm_adapters.py) zeigt den echten Harness mit
einem simulierten Verbindungsfehler und beiden Adapter-Varianten.

## Compaction-Policies und Kontextbudget

Compaction ersetzt einen alten, abgeschlossenen Gesprächspräfix durch eine
Zusammenfassung in der Modellprojektion. Das Original bleibt vollständig im
append-only Log. Jede angewendete Compaction ist genau ein `CompactionApplied`
mit Grenze `through`, Zusammenfassung, Policy-Name, geschützten Turn-IDs und
geschätzter Tokenzahl vor und nach der Anwendung.

| Policy | Wann sie zusammenfasst |
| --- | --- |
| `ThresholdCompaction(at=0.8)` | Request plus reservierte Output-Tokens erreichen 80 % des Kontextbudgets |
| `SlidingWindowCompaction(keep_turns=6)` | Ältere abgeschlossene Turns liegen außerhalb des wörtlich erhaltenen Fensters |
| `PeriodicCompaction(every=5)` | Seit dem letzten Compaction-Event sind fünf weitere Turns abgeschlossen |
| `ContextBudgetGuard(...)` | Prüft den fertigen Request und weist ihn bei Überschreitung vor dem Provider-Aufruf ab |

Für jeden automatischen Trigger stehen dieselben Retention-Optionen bereit:

| Option | Was wörtlich erhalten bleibt |
| --- | --- |
| `keep_turns=2` | Die letzten zwei abgeschlossenen Turns, zusätzlich zum laufenden Turn |
| `keep_first_turns=1` | Der erste Turn, etwa die ursprüngliche Aufgabenbeschreibung |
| `preserve_turns=(turn_id, ...)` | Explizit ausgewählte Turns, einschließlich ihrer Tool-Calls und Ergebnisse |
| `summary_tokens=2048` | Obergrenze für die neue Zusammenfassung nach dem konfigurierten TokenCounter |

Der aktuelle System-Prompt und der laufende Turn bleiben erhalten. Die Grenze
liegt immer hinter einem `TurnEnd`: Weder aktuelle Eingaben noch Tool-Paare
werden zerschnitten. Geschützte Turns werden nicht als neuer Originaltext an den
Summarizer übergeben und ihre IDs in weiteren automatischen Compactions
weitergeführt. Die Projektion kann sie aus dem Original-Log rekonstruieren.
System-Kontext aus `inject()` kann als Gesprächsinhalt zusammengefasst werden;
dauerhaft wörtliche Regeln gehören in `SystemPrompt.section()`.

```python
from cordify import (
    ContextBudgetGuard,
    PeriodicCompaction,
    SlidingWindowCompaction,
    ThresholdCompaction,
)
from cordify.presets import coding

policies = {
    "coding": ThresholdCompaction(at=0.8, keep_turns=2, keep_first_turns=1),
    "chat": SlidingWindowCompaction(keep_turns=6, keep_first_turns=1),
    "batch": PeriodicCompaction(every=5, keep_turns=2, summary_tokens=1200),
}
harness = coding(
    model,
    root="./repo",
    plugins=[policies["coding"], ContextBudgetGuard(reserve_tokens=4096)],
    context_window=64_000,
)
```

Die automatische Komposition schreibt höchstens ein Compaction-Event pro Step.
Sind mehrere Trigger montiert, bestimmt der zuerst wirksam werdende Trigger die
Retention dieses Events. Trigger, Retention und Budget-Guard lassen sich
kombinieren. Der gemeinsame Kern übernimmt frühere Zusammenfassungen in die
nächste Zusammenfassung und erhält zusätzlichen Kontext aus Request-Hooks.
Er legt nur dann ein Event an, wenn der gesamte Request dadurch kleiner wird.
Fehler oder Abbruch während der Zusammenfassung legen kein halbfertiges Event an.

`ContextBudgetGuard(limit=32_000, reserve_tokens=4096)` setzt zusätzlich eine
absolute Obergrenze; wirksam ist das Minimum aus `limit` und `context_window`.
Als Output-Reserve zählt das Maximum aus `request.max_tokens` und
`reserve_tokens`. Die Prüfung erfolgt nach `ConfigureRequest` und berücksichtigt
Nachrichten, Tool-Schemas und die Reserve. Passen geschützte Inhalte oder die
aktuelle Aufgabe weiterhin nicht hinein, erhält der Aufrufer
`ContextBudgetExceeded` mit `input_tokens`, `reserve_tokens` und `limit`.
Diese Inhalte werden zur Einhaltung des Budgets nicht still verworfen.

Der Guard ist ein reversibel montiertes Plugin. Ohne Guard bleibt
`context_window` die Planungsgröße für die gewählte Compaction-Policy.
Alle Größen richten sich nach dem `TokenCounter`; der Standard schätzt Tokens
über UTF-8-Bytes und erkennt keine Provider-Grenzen automatisch.
`ExtractiveCompactor` und `LLMCompactor` bleiben austauschbare Implementierungen
des `Compactor`-Protocols.

[`16_compaction_policies.py`](examples/16_compaction_policies.py) vergleicht die
drei Trigger offline, hält den ursprünglichen Auftrag wörtlich und prüft
unveränderte Log-Präfixe, Replay und Fork. Es zeigt außerdem einen Request, den
der Guard vor dem Provider-Aufruf stoppt.

## Rules und Kontextstrategien

```python
from cordify import (
    CacheBreakpoints,
    LatestObservationOnly,
    Rules,
    SpillLargeResults,
    ThresholdCompaction,
    when,
)

rules = Rules(
    [
        when(tool="run_command", command_contains="git push").steer(
            "Never push directly. Open a PR instead."
        ),
        when(tool="run_command", exit_code=lambda code: code != 0, repeated=3).inject(
            "Same command failed three times. Try a different approach."
        ),
        when(tool="read_file", result_tokens_over=20_000).note(
            "File is large. Use a line range next time."
        ),
    ],
    name="coding-policy-v1",
)

coding = Harness(
    [
        # agent_loop(), llm(...), eigene Tools ...
        rules,
        SpillLargeResults(max_tokens=8_000),
        ThresholdCompaction(at=0.8),
        CacheBreakpoints(),
    ],
    context_window=128_000,
)

browser = Harness(
    [
        # agent_loop(), llm(...), BrowserObserver(), browser_tools ...
        LatestObservationOnly(max_summaries=8),
        ThresholdCompaction(at=0.6),
    ]
)
```

`Rules` ist ein normales Klassenplugin mit einem `ReviewToolResult`-Hook. Aktionen
können verkettet werden und laufen in Deklarationsreihenfolge. `repeated=3` zählt
aufeinanderfolgende Matches je Tool und Befehl und löst beim dritten Match einmal
aus; ein Nicht-Match desselben Befehls setzt den Zähler zurück. Andere Befehle
haben eigene Zähler. Ohne `repeated` wird jedes Match behandelt. Der Befehl kommt
aus `ToolCall.command` (Argument `command` oder `argv`). `ToolResult.exit_code`
wird bei nativen `ProcResult`-Rückgaben automatisch übernommen und bleibt bei
`with_note()` und Spill erhalten.

Rules reagieren nach der Ausführung. Für das Verhindern einer Aktion werden
`ToolPolicy` oder `approval_gate` verwendet. Das manuelle `TestDiscipline`-Plugin
und die deklarative Variante stehen ausführbar in Beispiel 09; die async SDK-
Methoden `inject` und `steer` werden dabei jeweils mit `await` aufgerufen.

`RuleEvaluated` und `RuleTriggered` protokollieren Zähler und ausgelöste Aktionen.
Ein Resume stellt Zähler wieder her, ohne alte Aktionen erneut auszuführen.
Namen und Regelpositionen identifizieren die Policy: bei inhaltlichen Änderungen
einen neuen `name` wählen, ebenso für mehrere unabhängige Rules-Instanzen.

- `LatestObservationOnly` behält den aktuellen Zustand voll und höchstens
  `max_summaries` frühere Kurzfassungen je Quelle. Die Auswahl ist im Log
  festgehalten; Unload stellt die vorherige Einstellung wieder her.
- `SpillLargeResults` speichert große Ergebnisse im `SpillStore`. Das Modell
  bekommt Pfad, Hash, Vorschau und einen Hinweis auf das registrierte `read_spill`-
  Tool. Die Dateien bleiben für spätere Reads und Forks bestehen und sollten mit
  der Session aufbewahrt werden. Standardpfad: `.cordify/spills`.
- `ThresholdCompaction` prüft den vollständigen Request plus reservierte
  Output-Tokens. Ab `at * context_window` fasst es ältere abgeschlossene Turns
  zusammen und erhält standardmäßig die letzten zwei. Ein erfolgreicher Austausch
  schreibt genau ein `CompactionApplied`-Event. Replay und Fork leiten dieselbe
  Historie ab; laufende Turns, Eingaben und Tool-Paare werden nicht angeschnitten.
- `CacheBreakpoints` markiert das Ende des Prefix als exklusive Message-Position
  in `ModelRequest.cache_breakpoints`; volle aktuelle Observations bleiben im
  Tail. Es schreibt `CacheMetrics` nur aus gemeldeter Provider-Usage.

`TokenCounter`, `Compactor` und `SpillStore` sind austauschbare Services. Der
Standard zählt Tokens näherungsweise über UTF-8-Bytes und verwendet einen
`ExtractiveCompactor`, der Textauszüge ohne zusätzlichen Modellaufruf verdichtet.
`context_window` ist ein SDK-Budget (Standard 128000), keine automatisch erkannte
Modellgrenze. Für genaue Budgets einen passenden TokenCounter montieren.
Semantische Zusammenfassungen sind explizit möglich:

```python
from cordify import LLMCompactor, LocalSpillStore
from cordify.plugins.context.providers import compactor, spill_store
from cordify.plugins.observe.tokens import token_counter

# Zu den Harness-Plugins hinzufügen:
# compactor(LLMCompactor(summary_llm, model="your-summary-model"))
# token_counter(your_tokenizer)
# spill_store(LocalSpillStore("./artifacts/spills"))
```

`PrepareRequest` läuft nach Kontextaufnahme und Prompt-Rendering; dort arbeitet
Compaction. `ConfigureRequest` folgt danach und plant Cache-Metadaten auf dem
fertigen Request. Provider-Adapter entscheiden, wie sie Breakpoints umsetzen.
DeepSeek cached Prefixe automatisch und erhält keine erfundenen Cache-Parameter;
sein Adapter übernimmt die gemeldeten `prompt_cache_hit_tokens` und
`prompt_cache_miss_tokens`. Eine bestimmte Anzahl Cache-Misses ist nicht
garantiert. Siehe [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/).
