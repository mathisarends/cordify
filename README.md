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
    harness = Harness([
        agent_loop(), llm(ScriptedLLM(["Hallo aus Cordify."])),
        fs_local("."), workspace,
    ])
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

`Harness` montiert pro Session frische Contexts, ToolRegistry und SystemPrompt.
Session-Events und Modellwerte sind unveränderlich; Inbox und Modell-Historie
werden aus dem Log abgeleitet. `session_jsonl()` speichert JSONL mit einer
Versionsnummer und exklusivem OS-Schreiblock. `ModelRequested` enthält den
tatsächlichen Request, `AssistantChunk` ist nur ein Live-Event. Beim Resume werden
unterbrochene Turns geschlossen und fehlende Tool-Ergebnisse ergänzt.

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
