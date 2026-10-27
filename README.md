# Cordify

Ein natives Python-SDK für einen Agent-Harness: Context, Services, Plugins,
typisierte Events, Session-Log und ein austauschbarer ReAct-Loop. Inspiriert von
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) und
[Cordis](https://github.com/cordiverse/cordis), mit deren Begriffen und Python-APIs.
Keine API- oder Dateiformat-Kompatibilität mit den TypeScript-Projekten.

Python **3.12–3.14**, PEP-695-Generics, keine Runtime-Abhängigkeiten im Kern.
Das Paket liegt direkt in `cordify/`.

```sh
python -m pip install -e .
python -m examples.01_tool_agent
```

Ausführbare Beispiele (aus dem Repository-Root):

| Befehl | Zeigt |
| --- | --- |
| `python -m examples.01_tool_agent` | Tool-Loop, Token-Stream, weiterer Turn |
| `python -m examples.02_plugins` | Eigene Services, Konfiguration, Middleware, Cleanup |
| `python -m examples.03_inbox` | Follow-up, Steering und stille Injection während eines Laufs |
| `python -m examples.04_deepseek` | Echter DeepSeek-Adapter, Dateitools und JSONL-Resume |
| `python -m examples.05_bundles_and_injection` | Wiederverwendbare Module, Klassen, DI, Approval, Progress, Nudges |
| `python -m examples.06_explicit_registration` | Plugins und Hooks ohne Dekoratoren |
| `python -m examples.07_context` | Alle fünf Kontextwege: Sektionen, Aufgaben-Kontext, Anhänge, Push und Pull |
| `python -m examples.08_observations` | Browserzustand pro Step: voller aktueller Zustand, kompakte alte Zustände |

Nur Beispiel 04 benötigt einen API-Key und das `deepseek`-Extra. Die anderen
Beispiele verwenden deterministische Modellantworten, um das SDK sichtbar zu machen.

```python
import asyncio
from pathlib import Path

from cordify import Bundle, FS, Harness, Inject
from cordify.std import agent_loop, fs_local, llm
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

## Kontext: fünf Wege

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
`DENY` und `ALWAYS` (Freigabe für diesen Toolnamen bis zum Session-Ende).
Ein `ToolPolicy`-Hook kann `Decision.ASK` zurückgeben, um den Approver aufzurufen.
Die kompakte Form `approval_gate(risky={...}, ask=fn)` ist ebenfalls möglich.
`ToolResult.with_note(text)` reichert das Modell-Ergebnis an;
`await agent.inject(text)` fügt Kontext hinzu und `await agent.steer(text)` setzt
eine neue Step-Eingabe. `ToolReturned` beobachtet das bereits festgeschriebene Ergebnis.

`Harness` montiert pro Session frische Contexts, ToolRegistry und SystemPrompt.
Session-Events und Modellwerte sind unveränderlich; Inbox und Modell-Historie
werden aus dem Log abgeleitet. `session_jsonl()` speichert JSONL mit einer
Versionsnummer und exklusivem OS-Schreiblock. `ModelRequested` enthält den
tatsächlichen Request, `AssistantChunk` ist nur ein Live-Event. Beim Resume werden
unterbrochene Turns geschlossen und fehlende Tool-Ergebnisse ergänzt.

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
inkrementelle Projektionen und Compaction.

Sync-Tools laufen im Threadpool. Ein Timeout beendet dort das Warten, nicht den
Python-Thread. `fs_local` prüft auf Pfade außerhalb des Workspace, bietet aber
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

Diese erste Version führt Tools seriell aus. Sie enthält keinen MCP-Client,
Realtime-Transport, Docker-Backend, automatische Compaction oder Subagenten.
Die entsprechenden Erweiterungen können auf denselben Services und Events aufbauen.
