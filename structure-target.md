# Harnessed – Ziel-Ordnerstruktur

> **Status:** Vom Nutzer bestätigte Zielstruktur. Die Umsetzung erfolgt inkrementell; offene Arbeit steht in `review.md`. Ergänzt [`api-target.md`](api-target.md) (öffentliche API) und [`review.md`](review.md) (Zielbaum in Abschnitt 5). Der Paketname ist hier `harnessed`, wie im Code.

## Kurzfassung

- **Nach Feature ordnen, nicht nach technischer Rolle.** Heute verteilt sich z. B. das Dateisystem auf `seams/fs.py`, `adapters/fs.py` und `plugins/tools/fs.py`. Künftig steht alles zu einem Feature an einer Stelle: Protokoll, Standard-Implementierung und Plugin (Colocation).
- **Drei Ebenen statt sechs:**
  - `kernel/` ist der eigenständige Kernel.
  - `core/` enthält alles, was jeder Agent per Default lädt.
  - `plugins/` und `models/` sind opt-in.
  - Darüber liegt nur die Fassade `agent.py`. Presets gibt es nicht mehr: Ein Agent ist seine Plugin-Liste.
- **`seams/` und `adapters/` entfallen als Pakete.** Ein Protokoll liegt dort, wo es gebraucht wird: Braucht der Loop es, liegt es in `core/`, braucht es nur ein Feature, liegt es beim Feature.
- **Die großen Dateien werden aufgeteilt:** `core/loop.py` (908 Zeilen), `core/events.py` (506) und `core/session.py` (501).

---

## 1. Was heute stört

**Ein Feature, viele Orte.** Wer verstehen will, wie z. B. Spill funktioniert, muss vier Dateien in vier Ordnern lesen:

| Feature | Protokoll | Implementierung | Plugin | weitere |
|---|---|---|---|---|
| Dateisystem | `seams/fs.py` | `adapters/fs.py` | `plugins/tools/fs.py` | – |
| Shell | `seams/shell.py` | `adapters/shell.py` | `plugins/tools/shell.py` | – |
| Spill | `seams/spill.py` | `adapters/spill.py` | `plugins/_context/spill.py` | `plugins/_context/providers.py` |
| Compaction | `seams/compaction.py` | `adapters/compaction.py` | `plugins/_context/compaction.py` | `core/compaction.py`, `core/budget.py`, `plugins/_context/providers.py` |
| Approvals | `seams/approver.py` | `plugins/permissions/approval.py` | `plugins/permissions/defaults.py` | `core/approvals.py` |
| Persistenz | `core/session.py` (`SessionPersistence`) | `adapters/persistence.py` | `plugins/persistence.py` | – |
| Tokens | `seams/tokens.py` | `adapters/tokens.py` | `plugins/observe/tokens.py` | – |
| LLM | `seams/llm.py` | `adapters/base.py`, `deepseek.py`, `deepseek_models.py`, `sse.py` | `plugins/services.py` | `llms/`, `adapters/replay.py` |
| Prompt | – | `core/instructions.py`, `core/prompt.py`, `core/context.py` | `plugins/_context/prompt.py`, `instructions.py`, `dynamic.py` | – |

**Weitere Probleme:**
- **Doppelte und irreführende Namen:**
  - `kernel/events.py` und `core/events.py`;
  - `Plugin` und `Bundle` sind sowohl in `kernel/declarations.py` als auch in `core/features.py` definiert;
  - `core/context.py` (`DynamicContext`) neben dem Kernel-`Context`;
  - `seams/models.py` enthält Nachrichten, keine Models, und kollidiert mit dem neuen `Model`.
- **Willkürliche Unterordner in `plugins/`:**
  - `_context` ist privat, enthält aber öffentliche Plugins;
  - `agents/loop.py` hat 11 Zeilen;
  - `observe/` enthält Cache-Breakpoints;
  - `services.py` installiert Interna.
- **Die alte und die neue Fassade existieren parallel:** `presets.py` enthält `Harness`, `app/` enthält `Agent`, und `Agent` importiert `Harness`.
- **Platzhalter:** `llms/` (11 Zeilen) und `testing.py` (3 Zeilen). `ScriptedLLM` liegt dagegen in `adapters/replay.py`.
- **Die Tests liegen flach** in 40 Dateien und spiegeln den Code nicht.

---

## 2. Prinzipien

1. **Colocation:** Protokoll, Standard-Implementierung und Plugin eines Features liegen zusammen. Solange es klein ist, ist das *ein* Modul (`plugins/shell.py`). Erst wenn es wächst, wird es ein Paket.
2. **Protokolle liegen beim Verbraucher:** Ruft der Loop ein Protokoll auf (`Model`, `SessionStore`, `Approver`, `Summarizer`, `TokenCounter`), gehört es nach `core/`. Nutzt es nur ein Feature (`FileSystemBackend`, `Shell`, `SpillStore`), gehört es zum Feature.
3. **`core/` = was `Agent()` per Default installiert.** Das sind die Default-Slots aus `api-target.md` (`AgentLoop`, `MemorySessions`, `ToolOutputLimit`, `Approvals`, `NoopTracer`) und ihr Unterbau. Alles Opt-in liegt in `plugins/` oder `models/`.
4. **Features kennen sich nicht.** Ein Modul in `plugins/` importiert nie ein anderes Modul aus `plugins/`. Gemeinsames läuft über Protokolle in `core/`.
5. **Ein Name, ein Ort.** Jede öffentliche Klasse ist genau einmal definiert. Fassaden re-exportieren nur.

---

## 3. Zielbaum

```
harnessed/
  __init__.py              # facade: Agent, Session, RunResult, Plugin, tool, ... (~20 names)
  agent.py                 # Agent facade: resolves model=, instructions=, defaults, PluginConflict
  hooks.py                 # facade: re-exports core.hooks (Next, PrepareRequest, ToolPolicy, ...)
  events/                  # public namespaces: text, reasoning, tool, approval, ...

  kernel/                  # standalone: no harnessed imports, no third-party packages (unchanged)
    __init__.py            # Context, Inject, Key, plugin, hook, Event kinds
    context.py  di.py  effects.py  events.py  keys.py  plugin.py  declarations.py

  core/                    # agent runtime, everything Agent() loads by default; pydantic only
    messages.py            # Message, TextPart, ImagePart, ToolCall, ToolResult, user(), system()
    attachments.py         # File, Image, prepare_input
    model.py               # Model protocol, ModelInfo, ModelRequest/Response/Chunk/Stream, Usage
    tokens.py              # TokenCounter protocol + ApproximateTokenCounter (default)
    errors.py              # HarnessedError tree incl. ProviderError, ConfigError, PluginConflict
    event_registry.py      # session-local custom event catalogs and schemas
    plugin.py              # public Plugin base (@tool methods, guidance, slot), Service
    hooks.py               # hook events (Waterfall/Bail/Emit), Next
    stream.py              # public StreamEvent union (transient)
    tracing.py             # Tracer protocol + NoopTracer (slot "tracer")
    approvals.py           # Approver protocol, Approvals plugin, grants, CallbackApprover
    compaction.py          # mechanism + Summarizer protocol + session.compact(); budget
    tools/
      decorator.py         # @tool, ToolContext
      registry.py          # ToolRegistry, ToolNameConflict, ToolError
      arguments.py         # argument validation (was core/validation.py)
      scheduling.py        # parallel execution, resource keys
      output_limit.py      # ToolOutputLimit (slot "output_limit")
    prompt/
      instructions.py      # Instructions, Section, the guidance rule
      system_prompt.py     # agent.prompt, session.prompt.render()
      context.py           # ContextValue, session.context.set/remove
    session/
      session.py           # Session (append, fork, resume)
      events.py            # persistable log events
      codec.py             # versioned encode/decode (Pydantic union; one canonical format)
      store.py             # SessionStore protocol + MemorySessions (slot "sessions")
      projections.py       # messages, turns, inbox, approvals, status (incremental)
    runtime/               # was core/loop.py (908 lines)
      agent_loop.py        # AgentLoop plugin (slot "loop")
      stepper.py           # StepRunner: one model step
      executor.py          # ToolExecutor: approvals, execution, result review
      turns.py             # TurnRunner: turn continuation
      respond.py           # ModelResponder: provider stream and durable answer
      output.py            # OutputHandler: structured-output preparation/validation
      streaming.py         # stream projection and approval responder
      session.py           # public Session facade
      opener.py            # lease, seed and repair a log, then install its Session
      inbox.py             # run queue and steer
      control.py           # SessionStatus, pause/resume/stop/abort
      result.py            # RunResult, StopReason

  models/                  # provider integrations, everything behind extras
    __init__.py            # re-exports Model & co. from core.model; PydanticAIModel (SDK imported on use)
    pydantic_ai.py         # the only module that imports pydantic_ai
    context_windows.py     # table of known context windows

  plugins/                 # opt-in features, one module each, independent of each other
    __init__.py            # facade: every built-in plugin (Filesystem, Shell, Spill, ...)
    filesystem.py          # FileSystemBackend protocol, LocalFileSystem, Filesystem plugin (read_file, edit, ...)
    shell.py               # Shell protocol, LocalShell, Shell plugin (run_command)
    spill.py               # SpillStore protocol, local store, Spill plugin (read_spill)
    compaction.py          # Compaction plugin, at_ratio, every_turns, model summarizer
    sessions.py            # JsonlSessions
    # subagent.py          # later: Subagent; deferred from the current iteration
    rules.py               # Rules, when
    skills.py              # Skill, Skills (SKILL.md, load_skill, read_skill_file); yaml via harnessed[skills]
    observations.py        # LatestObservationOnly
    cache.py               # CacheBreakpoints

  integrations/            # later, as extras
    otel.py                # OtelTracer (harnessed[otel])
    mcp.py                 # MCP client (harnessed[mcp])

  testing/
    __init__.py            # ScriptedModel, InMemoryFS, FakeShell, reply, call, assert_tool_called
    models.py  fakes.py  assertions.py
    pytest_plugin.py
```

**Was entfällt ersatzlos:**
- `seams/`, `adapters/`, `app/`, `llms/`;
- `presets.py` mit `Harness`, `minimal`, `coding` und `headless`. Es gibt keine Presets mehr, weil ein Agent seine Plugin-Liste ist. Wer eine feste Zusammenstellung braucht, schreibt eine eigene Funktion, die einen `Agent` zurückgibt;
- `plugins/services.py` (`tools` und `system_prompt` stellt `AgentLoop` bereit, `llm` wird `Service(Model, x)`);
- `plugins/_context/providers.py`;
- `plugins/_context/instructions.py` (`persona` und `agents_md` werden `Instructions` bzw. `Section.from_file`);
- der eigene DeepSeek-Adapter samt `sse.py` und `RetryPolicy`.

---

## 4. Schichten und Architekturtest

| Rang | Schicht | Darf importieren |
|---|---|---|
| 0 | `kernel` | nur Standardbibliothek |
| 1 | `core` | `kernel`, `pydantic` |
| 2 | `plugins`, `models`, `integrations` | `kernel`, `core`, eigene Extras |
| 3 | `agent`, Fassaden (`__init__`, `hooks`, `events`) | alles darunter |
| – | `testing` | alles, wird aber von nichts importiert |

Der bestehende Test in `tests/test_architecture.py` (`LAYERS = {...}`) wird auf diese Tabelle umgestellt und um zwei Regeln ergänzt:
- **Features sind unabhängig:** Kein Modul in `plugins/` importiert ein anderes aus `plugins/`.
- **pydantic-ai bleibt gekapselt:** Nur `models/pydantic_ai.py` importiert `pydantic_ai`.
- **YAML bleibt gekapselt:** Nur `plugins/skills.py` importiert `yaml`.

Die Regeln aus `AGENTS.md` bleiben: absolute Imports über Paketgrenzen, `harnessed.kernel` als Fassade für Plugins, Ruff `TID252`.

**Späterer Sonderfall `Subagent` (zurückgestellt):** Diese Erweiterung wird in der aktuellen Iteration nicht gebaut. Falls sie später umgesetzt wird, bekommt das Plugin einen `Agent`, der eine Schicht höher liegt. `plugins/subagent.py` würde deshalb gegen ein kleines Protokoll in `core` (etwa `SessionFactory` mit `session()`) typisieren und `harnessed.agent` nicht importieren.

---

## 5. Öffentliche Importpfade

Die Pfade aus `api-target.md` bleiben fast gleich. Nur `seams` verschwindet:

| Import | Kommt aus |
|---|---|
| `harnessed` | `agent.py`, `core/*` (re-exportiert) |
| `harnessed.models` | `core/model.py` (Protokoll, Typen), `models/pydantic_ai.py` |
| `harnessed.plugins` | `plugins/*` und die Default-Plugins aus `core` (`AgentLoop`, `Approvals`, `ToolOutputLimit`, `Service`, `ContextValue`) |
| `harnessed.hooks`, `harnessed.events.*` | `core/hooks.py`, `core/stream.py`, `core/session/events.py` |
| `harnessed.kernel` | `kernel/` |
| `harnessed.testing` | `testing/` |

**Wer früher `harnessed.seams` brauchte:**
- Das Model-Protokoll kommt aus `harnessed.models`.
- Feature-Protokolle kommen aus dem jeweiligen Plugin-Modul, z. B. `from harnessed.plugins.shell import Shell, LocalShell`.

`api-target.md` (Spickzettel, Abschnitte 3.4 und 19) wird entsprechend nachgezogen.

---

## 6. Alt → neu

| Heute | Ziel |
|---|---|
| `seams/models.py` | `core/messages.py` (Nachrichten) und `core/model.py` (`ModelRequest`, `ModelChunk`, `ModelUsage`, `ToolDefinition`) |
| `seams/llm.py` | `core/model.py` (`Model`; `ManagedLLM` wahrscheinlich ersatzlos) |
| `seams/errors.py` | `core/errors.py` |
| `seams/approver.py` | `core/approvals.py` |
| `seams/compaction.py` | `core/compaction.py` (`Compactor` → `Summarizer`) |
| `seams/tokens.py`, `adapters/tokens.py`, `plugins/observe/tokens.py` | `core/tokens.py` |
| `seams/fs.py`, `adapters/fs.py`, `plugins/tools/fs.py` | `plugins/filesystem.py` |
| `seams/shell.py`, `adapters/shell.py`, `plugins/tools/shell.py` | `plugins/shell.py` |
| `seams/spill.py`, `adapters/spill.py`, `plugins/_context/spill.py` | `plugins/spill.py` |
| `adapters/base.py`, `deepseek.py`, `deepseek_models.py`, `sse.py` | entfallen (pydantic-ai) |
| `adapters/compaction.py` | `LLMCompactor` → `plugins/compaction.py`, `ExtractiveCompactor` → `testing/` |
| `adapters/persistence.py`, `plugins/persistence.py` | `plugins/sessions.py` |
| `adapters/replay.py` | `testing/models.py` (`ScriptedModel`) |
| `app/__init__.py` | `agent.py` |
| `core/loop.py` | `core/runtime/*` |
| `core/events.py` | `core/hooks.py` (Hook-Events) und `core/session/events.py` (Log-Events) |
| `core/session.py` | `core/session/*` |
| `core/inbox.py`, `control.py`, `turns.py` | `core/session/projections.py`, `core/runtime/inbox.py`, `core/runtime/control.py` |
| `core/features.py` | `core/plugin.py` (Plugin) und `core/tools/decorator.py` (`@tool`) |
| `core/tools.py`, `validation.py`, `scheduling.py`, `outputs.py`, `execution.py` | `core/tools/*` |
| `core/instructions.py`, `prompt.py`, `context.py`, `plugins/_context/prompt.py`, `dynamic.py` | `core/prompt/*` |
| `core/approvals.py`, `plugins/permissions/approval.py`, `defaults.py` | `core/approvals.py` |
| `core/compaction.py`, `budget.py` | `core/compaction.py` |
| `plugins/_context/compaction.py` | `plugins/compaction.py` (`ContextBudgetGuard` geht in `Compaction` auf, zu prüfen) |
| `plugins/_context/observations.py` | `plugins/observations.py` |
| `plugins/observe/cache.py` | `plugins/cache.py` |
| `plugins/permissions/rules.py` | `plugins/rules.py` |
| `plugins/agents/loop.py` | `core/runtime/agent_loop.py` |
| `presets.py` | entfällt (`Harness` und alle Presets) |
| `testing.py` | `testing/` |
| `llms/` | `models/` |
| – (neu) | `plugins/skills.py` (`api-target.md`, Abschnitt 12) |

---

## 7. Tests

Die Tests spiegeln den Code, damit man zu jedem Modul seine Tests findet:

```
tests/
  kernel/                  # test_context.py, test_di.py, test_events.py, ...
  core/
    runtime/  session/  tools/  prompt/
    test_approvals.py  test_compaction.py  ...
  plugins/                 # test_filesystem.py, test_shell.py, test_spill.py, test_skills.py, ...
  models/                  # test_pydantic_ai.py (skipped without the extra)
  test_agent.py            # facade, PluginConflict, desugaring
  test_architecture.py     # layers, feature independence, pydantic_ai isolation
  test_examples.py
  typing/                  # assert_type cases
```

---

## 8. Offene Punkte

- **`kernel.Plugin` und das öffentliche `Plugin`:** Heute gibt es zwei Klassen mit dem Namen `Plugin`. Vorschlag: Das öffentliche `Plugin` (mit `@tool`, `guidance`, `slot`) lebt in `core/plugin.py`. Die Kernel-Basis bekommt einen eigenen Namen, oder `core.Plugin` erbt sie nur. Zu entscheiden, bevor umgezogen wird.
- **`core/` bleibt groß.** Mit `runtime/`, `session/`, `tools/` und `prompt/` als Unterpaketen ist es aber gegliedert. Die Alternative wäre, `session/` und `runtime/` auf die oberste Ebene zu heben. Das lohnt sich erst, wenn jemand sie ohne den Rest nutzen will.
- **Alternative ohne Feature-Colocation:** Die heutige Schichtung (`seams`/`adapters`/`plugins`) bleibt, und es wird nur aufgeräumt (Namen, große Dateien, `_context`). Das macht weniger Umbau, aber das Grundproblem aus Abschnitt 1, ein Feature an vielen Orten, bleibt.
- **Paketname** `harnessed` oder `harnessed`: unabhängig von dieser Struktur.

## 9. Reihenfolge der Umsetzung

1. Architekturtest auf die neue Tabelle umstellen (zunächst mit Ausnahmen für den Altbestand).
2. `seams/models.py` → `core/messages.py` + `core/model.py` (größter Fan-out, deshalb zuerst, mit Re-Export am alten Ort).
3. Features einzeln zusammenziehen: Filesystem, Shell, Spill, Sessions, Tokens.
4. `core/loop.py`, `core/events.py` und `core/session.py` aufteilen (gemeinsam mit P0-1 und P1-1 aus der Review).
5. `models/pydantic_ai.py` einführen, DeepSeek-Adapter und `adapters/` entfernen.
6. `Harness` und `presets.py` löschen (Beispiele und Tests auf `Agent` umstellen), Re-Exports am alten Ort löschen, Tests spiegeln.

## Ergänzung: bestätigte DX-Erweiterungen

Tools werden öffentlich ausschließlich als Methoden eines Plugin registriert.
Config-Modelle stehen auf Modulebene und werden durch explizite Konstruktoren sowie
`Config = MyConfig` und `config: MyConfig` angebunden. Das öffentliche `events/`-Paket
enthält nur Namespace-Fassaden; Event-Werte und die sessionlokale EventRegistry gehören
zum Core. `core/runtime/handles.py` enthält RunHandle und dessen Completion-Zustand;
Submit und Run verwenden den bestehenden Session-Driver. Pure History-Folds leben in
`core/session/history.py`, sodass JSON-fähige Ergebnis-Events den veränderlichen
SessionLog nicht importieren müssen. Der Kernel bleibt standardbibliotheksbasiert.

## Umgesetzte Runtime- und API-Konventionen

`TurnRunner` und `StepRunner` besitzen die Ablaufsteuerung. `ModelResponder`,
`ToolExecutor` und `OutputHandler` erhalten ihre konkreten Abhängigkeiten;
`RunResults` materialisiert Antworten. `Session` besitzt die öffentlichen Controls,
Subscriptions und Run-Handles. Freie Funktionen nehmen keine Session als
Pseudo-`self` entgegen. `project_stream_events` projiziert den Rohstream mit
Klassenmustern und einer expliziten Statusabbildung.

`PromptComposition` verarbeitet normalisierte `PromptContribution`-Werte.
Lifecycle-Registrierungen laufen durch `Context.register` und
`RegistrationState.active`. Öffentliche Feature-Facades verwenden direkte
Reexports. Nur der SDK-Root bleibt lazy, damit `harnessed.kernel` ausschließlich
die Standardbibliothek benötigt; `__init__.pyi` liefert seine statischen Exporte.

Das unreleased Speicherformat hat ausschließlich Version 1 und dotted Event-Tags.
Output-Verträge werden anhand registrierter Python-Validatoridentität aufgelöst;
ein gleiches JSON-Schema allein genügt ausdrücklich nicht.
