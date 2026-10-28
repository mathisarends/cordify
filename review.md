# Harnessed – Review implementation tracker

**Implementation status (2026-10-28):** Findings below describe the reviewed snapshot, not necessarily the current code. Struck-through findings have been fixed and verified; partial findings remain open. Existing SDK entry points stay supported while improvements are introduced. The optional retry helper keeps the explicitly requested name `LLM`; OpenTelemetry remains deferred.

- P1-6 (partial): duplicate session installation and duplicate cleanup removed; presets now use public `Context.install(..., settle=True)`.
- P1-3: resolved — registration-time binding plans, weak callable cache, no DI reflection in listener/tool dispatch.
- 1.2 optional/named injection: resolved — `Inject[T | None]` and `Annotated[T, InjectMarker(key)]`. Optional injection does not restart plugins.
- P1-4 (partial): immutable plugin keyword configuration. P1-9 (partial): cached tool schemas and redundant validation removed.
- Remaining findings: open; implementation and verification are in progress.

**Stand:** 2026-10-28 · Commit `c78d4fd` plus lokal gestagte CI-Änderungen (pre-commit, ty, uv.lock)
**Umfang:** gesamtes Paket `harnessed/` (kernel, seams, core, plugins, adapters, presets), README und Beispiele
**Maßstab:** ein production-grade SDK, mit dem man moderne, anspruchsvolle Agents baut, und zwar gerne
**Status:** 431 Tests grün (16,7 s), `ruff` und `ty check --error-on-warning` sauber, CI über 3 OS × Python 3.12–3.14 plus Wheel-Check

**Lesehilfe:** Jeder Befund endet mit **→ Empfehlung**. Jeder Befund steht genau einmal im Dokument; an anderen Stellen wird nur per ID (P0-1, P1-8, …) oder Abschnitt verwiesen.

---

## 0. TL;DR

Harnessed ist technisch sehr sorgfältig gebaut. Der Kernel ist sauber, die Schichten werden per Test erzwungen, das Session-Log ist durable, und an fast jeder Grenze wird validiert. In dieser Form ist es aber **noch kein SDK, das Leute gerne benutzen**. Die vier wichtigsten Probleme:

1. **API zu breit und zu intern.** Für ein Hello World braucht man ~90 Exports, vier Konzeptebenen und doppelte Namen. → `Agent(llm=, instructions=, plugins=)` als reiner Zucker über Plugins, Tools nur als `@tool`-Methoden in Plugins, ~20 Top-Level-Namen (P0-7, 3.1).
2. **Laufzeit skaliert nicht.** → Token-Chunks nur noch emittieren statt persistieren (P0-2), Projektionen inkrementell fortschreiben (P0-1), Writes über einen Hintergrund-Writer aus dem Event-Loop nehmen (P0-3).
3. **Seams zu schmal und DeepSeek-gefärbt.** → Vor 1.0 erweitern: multimodales `ToolResult`, `stop_reason`, Model-Settings, neutrale Namen (P0-6).
4. **Betriebsreife fehlt.** → Logging mit `NullHandler`, ein `Tracer`-Slot (OTel-Exporter später), `ASK` als Default für Tools mit Seiteneffekten, gehärtete Shell, `error_formatter` (P0-4, P0-5).

**Scope** (Abschnitt 4): Mechanismen in den Core, *eine* gute Policy als Opt-in, Adapter als Extras, Spielzeug nach `testing`. Compaction: ja, einen Default offenlegen, aber als *eine* Klasse mit LLM-Summarizer statt vier Klassen plus Extractive-Default.

### Bewertung je Bereich

| Bereich | Note | Kurzbegründung → Fix |
|---|---|---|
| Kernel (Context, DI, Events, Effects) | **B+** | Sauber. Reflection pro Aufruf, unsichtbarer Owner → P1-3, P1-5 |
| Session-Log und Durability | **B** | Bestes Konzept im Paket; Projektionen werden neu gefaltet, Chunks persistiert, Writes blockieren den Event-Loop → P0-1, P0-2, P0-3 |
| Agent-Loop (`core/loop.py`) | **C** | God-Class, Flag-State, dünnes `RunResult` → P1-1, P1-2 |
| Seams und Datenmodelle | **C+** | Typisiert und frozen, aber zu schmal → P0-6 |
| Built-in Plugins | **C+** | Drei Autorenstile, No-op- und Spielzeug-Plugins → P1-4, Abschnitt 4 |
| Adapter | **C** | Nur DeepSeek, kein Pooling, keine typisierten Fehler → P1-10 |
| API-Ergonomie | **C−** | Hello World braucht Imports aus 5 Modulen → P0-7, Abschnitt 3 |
| Konventionen und Typing | **B** | Ruff, ty und pre-commit in CI. Offen: zwei Validierungssysteme nebeneinander → P1-8 |
| Tests | **A−** | 431 Tests, OS-Matrix, Beispiele laufen in CI mit. Offen: `harnessed.testing` für Nutzer → P2-6 |
| Doku | **B−** | Inhaltlich vollständig, aber 1548 Zeilen README → README kompakt, Rest nach `/docs` (P2-8) |

---

## 1. Ziele und Konzeptadaption (DSH- und Cordis-Port)

### 1.1 Übernommene Konzepte

| Konzept | Herkunft | In Harnessed | Urteil |
|---|---|---|---|
| Everything is a plugin | Cordis, DSH | Harness als Liste von Plugins, auch Loop und LLM | ✅ Das wichtigste Prinzip, behalten. **→ Empfehlung:** Die neue `Agent`-Fassade bildet 1:1 darauf ab und führt kein zweites Erweiterungskonzept ein (3.1). |
| Context mit Services, Plugins und reversiblen Effects | Cordis | `kernel/context.py`, `kernel/effects.py` | ✅ Sauber und pythonisch (async context manager, Disposer). |
| Temporale `needs` (waiting → active → stopping) | Cordis `inject` | `_Runtime`-Reconcile plus `ctx.settle()` | ✅ `settle()` als explizite Barriere ist eine gute Python-Übersetzung. |
| Event-Semantik emit/bail/waterfall/parallel | Cordis | `Emit`, `Bail[R]`, `Waterfall[R]`, `Parallel` | ✅ Besser als das Original: Die Semantik hängt am Event-Typ, nicht an der Aufrufstelle. |
| Append-only Log, alles andere als Projektion | DSH | `core/session.py` | ✅ Das richtige Fundament für Resume, Fork und Audit. |
| Inbox-Modi followup/steer/inject, pause/resume/stop | DSH | `core/inbox.py`, `core/control.py` | ✅ Treu portiert und durable. |
| Approvals mit ALWAYS-Grants | DSH | `core/approvals.py` | ✅ Inklusive Revoke und Fork. |
| Output-Limit, Spill, Compaction, Rules, Observations | DSH | `plugins/context/*`, `plugins/permissions/rules.py` | ⚠️ Funktional portiert, aber als Top-Level-Features exportiert. **→ Empfehlung:** `ToolOutputLimit` bleibt Core und ist per Default an. Spill wird ein eingebauter, session-gebundener Store mit Retention (in `coding` an). Compaction wird *eine* Klasse `Compaction(trigger=…, summarizer=…)` mit dem Mechanismus im Core. `Rules` wandern nach `harnessed.plugins` und persistieren nur noch bei Triggern. Observations werden ein Opt-in-Plugin. Begründung in 4.2 und 4.3. |

### 1.2 Wo die Adaption nicht stimmt

- **`ctx.on()` gibt die Funktion zurück, `add_listener()` einen Disposer** (`kernel/context.py:137`/`148`). In Cordis liefert `ctx.on` den Disposer, das Kernidiom für reversible Effects.
  **→ Empfehlung:** Als Decorator (`@ctx.on(Event)`) gibt `on` die Funktion zurück. Als Aufruf (`ctx.on(Event, fn)`) gibt es einen Disposer zurück, analog zu `ToolRegistry.register`. `add_listener` entfällt.
~~- **Kein optionales Inject.** Cordis unterscheidet `required` und `optional`. Hier sind `needs` immer hart, sodass ein Plugin wie „nutze den TokenCounter, falls vorhanden“ nicht ausdrückbar ist.~~
  **→ Empfehlung:** `Inject[T | None]` bzw. `needs=[Optional(T)]`. Der Wert ist `None`, solange der Service fehlt, und das Plugin wird **nicht** neu gestartet, wenn er auftaucht. Wer Reaktivität braucht, nimmt weiter ein hartes `needs`.
- **Kein Config-Schema für Plugins.** Cordis hat `Schema`. Harnessed validiert Konfiguration von Hand.
  **→ Empfehlung:** Pydantic-Config-Modelle pro Plugin, siehe P1-8.
~~- **`Inject[...]` akzeptiert nur `type`, `needs` dagegen auch `Key[T]`** (`kernel/di.py:27-33`). Damit ist `Key` zweitklassig.~~
  **→ Empfehlung:** `Inject[Key[T]]` bzw. `Annotated[T, Inject(key)]` unterstützen und per Test absichern, dass jede Form, die `needs` kennt, auch injizierbar ist.
- **DSH- und OpenAI-Wire-Begriffe in den generischen Seams** (`reasoning_content`, `prompt_cache_*`, `image_url`, `ToolCall.command`). Ein Port sollte die *Ideen* von DSH übernehmen, nicht dessen Wire-Format.
  **→ Empfehlung:** Umbenennen und verschieben, Details in P0-6.
- **`CacheBreakpoints` ist ein No-op.** Das Plugin setzt `ModelRequest.cache_breakpoints` (`plugins/observe/cache.py:34`), aber **kein Adapter liest das Feld**. DeepSeek cached den Prefix ohnehin automatisch. Das Feature suggeriert eine Wirkung, die es nicht hat.
  **→ Empfehlung:** Plugin und `CacheMetrics`-Event entfernen. Cache-Platzierung entscheidet der Adapter (Anthropic explizit, DeepSeek/OpenAI automatisch). Cache-Zahlen stehen schon in `ModelUsageRecorded`.

### 1.3 Ziel-Check: „ultra production-grade, cleane API“

| Anforderung | Stand | → Empfehlung |
|---|---|---|
| Hello World in ≤ 10 Zeilen mit einem Import | ❌ Beispiel 01 importiert aus 5 Modulen | 3.1, P0-7 |
| Typisierte Ergebnisse (Output, Usage, Stop-Reason) | ❌ `RunResult(text, events)` | P1-2 |
| Typisierter Event-Stream für UIs | ❌ rohe Log-Events (~30 Typen) | 3.2 |
| Structured Output | ❌ | 3.3 |
| Multimodale Tool-Results (Computer Use, Screenshots) | ❌ `ToolResult.content: str` | P0-6 |
| Subagents / Handoffs | ❌ | 4.3 |
| MCP | ❌ | 4.3 |
| Logging / Tracing / OTel | ❌ kein einziges `logging` | P0-4 |
| Mehr als ein Provider | ❌ nur DeepSeek plus `ScriptedLLM` | P1-10 |
| Sichere Defaults | ❌ `ToolPolicy` per Default `ALLOW` | P0-5 |
| Durable Sessions, Resume, Fork | ✅ stärker als bei den meisten Frameworks | behalten (Abschnitt 7) |
| Erweiterbarkeit über Hooks | ✅ sehr stark | behalten (Abschnitt 7) |

**Fazit:** Das Fundament (Kernel plus Log) ist besser als bei vielen populären Agent-SDKs. Die Oberfläche und die Breite der Seams liegen deutlich dahinter. **→ Empfehlung:** In der nächsten Phase keine weiteren DSH-Features portieren, sondern die Oberfläche ergonomisch machen und die Seams auf heutige Agents erweitern (Roadmap in Abschnitt 6).

---

## 2. Findings nach Priorität

### P0: vor jeder öffentlichen 1.0 beheben (Breaking oder Betriebsrisiko)

#### P0-1 · Projektionen sind O(n) pro Zugriff, also O(n²) pro Session
- `session.messages`, `.inbox`, `.turns`, `.control` und `Approvals(...)` falten bei *jedem* Zugriff das komplette Log neu.
- `Session.append` baut zur Validierung von Inbox- und Approval-Events `Inbox(...)` und `Approvals(...)` über das ganze Log neu auf.
- Plugins scannen zusätzlich `agent.session.events`: `PreStep.latest_user_text`, Compaction (`plugins/context/compaction.py:51-60`), `CacheBreakpoints` und `Rules.setup`.

**→ Empfehlung:** Inkrementelle Reducer. Jede Projektion ist ein reiner Fold, den `Session` beim Append fortschreibt. Die Neuberechnung aus dem vollen Log bleibt für `load`, `fork` und als Test-Orakel.

```python
class Projection[S](Protocol):
    def initial(self) -> S: ...
    def apply(self, state: S, event: SessionEvent) -> S: ...  # pure, O(1) amortized


class Session:
    async def append(self, event: SessionEvent) -> None:
        for p in self._projections:          # validate against current state first
            p.validate(self._state[p], event)
        self._log.append(event)
        for p in self._projections:
            self._state[p] = p.apply(self._state[p], event)
        ...

    @property
    def inbox(self) -> InboxState:          # O(1)
        return self._state[INBOX]

    def last[E: SessionEvent](self, kind: type[E]) -> E | None: ...  # index per type
```

- Ein Property-Test stellt sicher, dass `fold(events) == incremental(events)` für zufällige Logs gilt. Damit bleibt die heutige Korrektheit erhalten.
- `session.last(StepStart)` und `session.since(StepStart)` ersetzen das Rückwärtsscannen in den Plugins.

#### P0-2 · Jeder Token-Chunk ist ein persistiertes Event
- `core/loop.py:733-736` hängt für jeden Stream-Chunk ein `AssistantChunk` an das Log.
- Pro Token heißt das: ein JSON-Encode, ein Datei-Write mit Flush (optional `fsync`), zwei Bus-Dispatches (`SessionAppended` plus Event) und zweimal DI-Reflection (P1-3).
- Gebraucht werden die Chunks heute nur für zwei Dinge: für den Live-Stream (`Agent._session_event` → Subscriber-Queues) und für `_pending_assistant_messages`, das nach einem Abbruch den Teiltext rekonstruiert (`core/session.py:131`).

**→ Empfehlung:** Chunks werden nur noch **emittiert**, nicht mehr persistiert. Zwei klar getrennte Kanäle:

```python
class Session:
    async def append(self, event: SessionEvent) -> None: ...   # durable: log + projections + bus
    async def publish(self, event: TransientEvent) -> None: ... # bus/stream only, never persisted


@dataclass(frozen=True)
class TextDelta(TransientEvent):        # replaces AssistantChunk in the log
    message_id: str
    text: str = ""
    reasoning: str = ""
```

- Der Model-Stepper sammelt den Teiltext in einem In-Memory-Puffer. Am Ende des Streams persistiert er genau ein `AssistantMessage`. Bei Cancel oder Fehler persistiert er `AssistantMessage(partial, interrupted=True)` aus dem Puffer. Das Event gibt es schon, nur die Quelle ändert sich.
- `_pending_assistant_messages` und der Partial-Zweig in `_derive_message_entries` entfallen. Das Log enthält nur noch fertige Nachrichten.
- `stream()` abonniert beide Kanäle. Damit sieht eine UI weiterhin jeden Token, das Log aber nicht.
- Der einzige Verlust ist der Teiltext bei einem **harten** Prozess-Crash mitten im Stream. Der wird ohnehin neu generiert. Wer ihn trotzdem braucht, bekommt einen optionalen Checkpoint (`AssistantPartial` alle N Sekunden), der standardmäßig aus ist.

#### P0-3 · Blockierende I/O im Event-Loop
- `JsonlPersistence.load` liest synchron. `append` schreibt, flusht und fsynct synchron, obwohl die Methoden `async def` sind (`adapters/persistence.py:27-105`, `134-144`). Jeder `fsync` hält damit **alle** Coroutinen an: Streams, parallele Tools und andere Sessions im selben Prozess.
- `LocalSpillStore` und `LocalFS` lagern dagegen über `to_thread` aus. Das ist inkonsistent.

**→ Empfehlung:** Ein Hintergrund-Writer pro Session mit Group-Commit:

```python
class JsonlPersistence:
    def __init__(self, root: Path, *, durability: Literal["buffered", "flush", "fsync"] = "flush"): ...

    async def append(self, session_id: str, seq: int, event: SessionEvent) -> None:
        self._queue.put_nowait((seq, encode(event)))   # O(1), keeps seq order

    async def flush(self) -> None:                     # awaits until everything enqueued is durable
        ...

    async def _writer(self) -> None:                    # one task per open session
        while batch := await self._drain():             # take everything currently queued
            await asyncio.to_thread(self._write_batch, batch)  # write + flush/fsync once per batch
```

- **Reihenfolge:** Eine Queue mit einem einzigen Writer garantiert die `seq`-Ordnung ohne Lock.
- **Durability-Garantie:** `append` kehrt zurück, sobald das Event in der Queue steht. An den Stellen, an denen Durability semantisch zählt, wartet der Loop explizit auf `await persistence.flush()`: vor der Ausführung eines Tools mit Seiteneffekten (damit `ToolStarted` sicher im Log steht), am Turn-Ende und in `aclose()`.
- **Fehler:** Ein Write-Fehler im Writer markiert die Session als `failed`. Der nächste `append` oder `flush` wirft ihn, statt ihn zu verschlucken.
- `load` läuft komplett in `asyncio.to_thread`.
- Das `Persistence`-Protocol bekommt `flush()` und `aclose()`. Damit können SQLite- oder Postgres-Adapter später dasselbe Modell nutzen.

#### P0-4 · Keine Observability
- `grep logging|getLogger|warnings.warn` findet **null** Treffer im Paket.
- Fehler beim Hintergrund-Setup von Plugins sieht man nur, wenn jemand `ctx.settle()` aufruft (`kernel/context.py:201-212`). Sonst bleiben sie stumm.
- Es gibt keine Spans für Model-Call, Tool-Call, Turn oder Compaction.

**→ Empfehlung:** drei Bausteine.

1. **Logging nach Bibliothekskonvention.**
   - Pro Modul `log = logging.getLogger(__name__)`. In `harnessed/__init__.py` wird nur `logging.getLogger("harnessed").addHandler(logging.NullHandler())` gesetzt, **nie** `basicConfig`.
   - Levels:
     - `ERROR` für fehlgeschlagene Plugin-Setups, und zwar sofort, nicht erst bei `settle()`.
     - `WARNING` für verworfene Compaction (4.2) und Retries.
     - `DEBUG` für Hook-Reihenfolge und DI-Auflösung.
   - Tool-Exceptions landen mit `exc_info` im Log. Das Modell bekommt nur den Text aus dem `error_formatter` (P0-5).
2. **`Tracer`-Seam im Core**, als Plugin-Slot `tracer` mit einem No-op als Default:
   ```python
   class Tracer(Protocol):
       def span(self, name: str, **attrs: Any) -> AbstractAsyncContextManager[Span]: ...

   async with tracer.span("harnessed.model", model=req.model) as span:
       ...
       span.set(input_tokens=usage.input, output_tokens=usage.output, stop_reason=stop)
   ```
   Spans gibt es für `turn`, `step`, `model`, `tool` (mit Tool-Name und Status), `compaction` und `approval.wait`.
3. **Später, als Erweiterung:** ein Plugin `OtelTracer` im Extra `harnessed[otel]`, das die OpenTelemetry GenAI Semantic Conventions bedient (`gen_ai.request.model`, `gen_ai.usage.*`, `gen_ai.tool.name`). Weil Seam und Span-Punkte aus Schritt 2 schon existieren, kommt es ohne Breaking Change dazu. Für 1.0 nicht nötig.

Zusätzlich gibt es ein öffentliches Event `PluginFailed(plugin, error)`, auf das Hosts reagieren können, etwa für Alerts oder Restarts.

#### P0-5 · Unsichere Defaults und Leaks
- **Der Policy-Default ist `ALLOW`** (`core/loop.py:661`). `coding(shell=True)` lässt das Modell beliebige Executables ohne Rückfrage ausführen.
  **→ Empfehlung:**
  - Tools deklarieren ihre Wirkung: `@tool(readonly=True)` bzw. `@tool(side_effects=True)`. Der Default ist `side_effects=True`, also im Zweifel sicher.
  - Die Default-Policy lautet: `readonly` → `ALLOW`, sonst `ASK`.
  - Ohne konfigurierten Approver führt `ASK` zu einem klaren `ApprovalRequired`-Fehler beim Start, nicht zu einem Hänger.
  - Der Opt-out ist explizit: `Approvals(trust="all")` bzw. `trust=["write_file"]`.
  - Die eingebauten FS-Lese-Tools sind `readonly`.
- **`LocalShell`** (`adapters/shell.py:13-36`):
  - Beim Timeout wird nur der direkte Child-Prozess gekillt, der Prozessbaum bleibt stehen.
  - Es gibt kein Output-Cap: `communicate()` puffert alles im RAM.
  - Den `timeout` setzt das Modell, ohne Maximum.

  **→ Empfehlung:**
  - Prozessgruppe starten: POSIX `start_new_session=True` und beim Abbruch `os.killpg(pgid, SIGTERM)`, nach einer Grace-Period `SIGKILL`. Windows `CREATE_NEW_PROCESS_GROUP` plus Job-Object mit `KILL_ON_JOB_CLOSE`.
  - stdout und stderr inkrementell lesen und nach `max_output_bytes` (Default 1 MB) abschneiden, mit einem Truncation-Marker.
  - `LocalShell(max_timeout=600)` begrenzt den vom Modell gewählten Wert.
  - Optional `env=`-Allowlist und `cwd`-Jail.
- **Exception-Texte gehen ans Modell:** `ToolResult.error(f"{type(exc).__name__}: {exc}")` (`core/tools.py:203`, `core/loop.py:678`). Das leakt Pfade, Connection-Strings und Secrets in den Prompt, ins Log und zum Provider.
  **→ Empfehlung:**
  - Ein `error_formatter` als Option des Loop-Plugins (`agent_loop(error_formatter=...)`). Der Default ist `"{tool} failed: {ExceptionType}"`.
  - Mit `ToolError("message for the model")` kann ein Tool bewusst eine Nachricht ans Modell geben.
  - Die volle Exception geht ins Logging (P0-4).
- **Falsche Attribution:** `"User declined this action"` (`core/loop.py:673`) steht auch dann, wenn eine *Policy* abgelehnt hat.
  **→ Empfehlung:** `ToolResult.denied(by="policy" | "user", reason=...)` mit unterschiedlichen Texten ans Modell.

#### P0-6 · Seams sind zu schmal (Breaking, also jetzt ändern)
- **`ToolResult.content: str`.** Damit gibt es keine Bilder und keinen Mischinhalt, also weder Computer Use noch Browser-Screenshots noch PDF-Seiten.
  **→ Empfehlung:** `content: str | tuple[ContentPart, ...]`, analog zu `Message`. Adapter, die keine Bilder in Tool-Results können, degradieren zu einem Text-Platzhalter.
- **`ModelChunk` hat kein `finish_reason`.** Ein Abbruch wegen `max_tokens` ist nicht erkennbar, und der Loop hält die abgeschnittene Antwort für fertig. Der DeepSeek-Wire-Typ hat das Feld (`adapters/deepseek_models.py:189`), es wird aber verworfen.
  **→ Empfehlung:**
  - `stop_reason: Literal["end", "tool_calls", "max_tokens", "content_filter", "error"]` im letzten Chunk.
  - Der Loop reagiert auf `max_tokens` konfigurierbar mit `continue`, `fail` oder `accept`.
  - Der Wert landet in `RunResult.stop_reason`.
- **`ModelRequest`** hat kein `temperature`, `top_p`, `tool_choice`, `response_format`, `reasoning_effort`, `stop`, `parallel_tool_calls` und keinen Escape-Hatch.
  **→ Empfehlung:** `settings: ModelSettings` (typisiert, alle Felder optional) plus `provider_options: Mapping[str, Any]`, das der Adapter unverändert durchreicht.
- **Provider-Begriffe:** `reasoning_content`, `prompt_cache_hit_tokens`/`miss`, `ImagePart.type="image_url"`.
  **→ Empfehlung:** Umbenennen in `reasoning`, `cache_read_tokens` und `cache_write_tokens`. Das Feld `ImagePart.type` entfällt, weil der Typ der Klasse genügt.
- **`ToolCall.command`** ist eine Shell-Eigenschaft auf einem generischen Werttyp; `Rule.matches` hängt daran (`plugins/permissions/rules.py:40`, `92`).
  **→ Empfehlung:** Raus aus `ToolCall`. Rules matchen über `call.arguments` oder einen Extractor, den das Shell-Plugin bereitstellt.
- **`Decision` vermischt Policy und Antwort.** `ASK` ist nur als Policy-Ergebnis sinnvoll, `Approver.ask` darf es aber typgemäß zurückgeben.
  **→ Empfehlung:** Zwei Enums, `PolicyDecision {ALLOW, DENY, ASK}` und `ApprovalAnswer {ALLOW, DENY, ALWAYS}`.
- **`FS`** kennt nur `read` und `write`.
  **→ Empfehlung:** `list`, `glob`, `stat`, `exists`, `delete` und `replace(path, old, new)` ergänzen. `read` bekommt `offset`/`limit`, damit `read_file` nicht die ganze Datei lädt (heute `plugins/tools/fs.py:25`).
- **Inkonsistente Typen:** `Shell.run(argv: list[str])` neben Tupeln überall sonst; `SpillStore.read(path: str)` neben `FS` mit `Path`.
  **→ Empfehlung:** Überall `Sequence[str]` und `PurePath`.
- **`TokenCounter`** ist synchron, echte Provider-Tokenizer sind I/O.
  **→ Empfehlung:** `async def count(...)`, wobei der Approximator synchron bleiben darf.
- **Es gibt kein `LLM.context_window`.** 128k sind hart im Harness kodiert (`presets.py:21`).
  **→ Empfehlung:** `LLM.info -> ModelInfo(name, context_window, max_output, supports_images, …)`. Budget und Compaction lesen daraus.

#### P0-7 · Public API ist zu breit, Namen kollidieren
- `harnessed/__init__.py` exportiert ~90 Namen, darunter Kernel-Interna (`InjectMarker`, `PluginSpec`, `Emit`), Adapter und mit `AgentHandle = Agent` (`core/loop.py:762`) einen Alias ohne Mehrwert.
- **Namenskollisionen:**
  - `Plugin` und `Bundle` gibt es zweimal, einmal in `kernel.declarations` und einmal in `core.features`.
  - `LLM` gibt es zweimal: als Retry-Basisklasse in `adapters.base` und als Protocol in `seams.llm`. In `adapters/deepseek.py:12` ist die Basisklasse gemeint, in `plugins/services.py` das Protocol.
- **Die Exportliste wird dreifach gepflegt:** `TYPE_CHECKING`-Imports, `_EXPORTS` und `__all__`.

**→ Empfehlung:**
- Zielstruktur aus 3.5 mit ~20 Top-Level-Namen.
- `adapters.base.LLM` wird zu `llms._base.StreamingLLM`. `Plugin` und `Bundle` gibt es nur noch in `core`, der Kernel behält Protocol und Mechanik. `AgentHandle` entfällt.
- Ein Test prüft, dass `__all__` sortiert ist und den Lazy-Exports entspricht. Ein zweiter Test prüft, dass kein öffentlicher Name zweimal unter verschiedenen Objekten existiert.

---

### P1: vor einer Beta beheben

#### P1-1 · `Agent` ist eine God-Class mit impliziter State-Machine
- `core/loop.py` hat 762 Zeilen: Inbox, Run-Control, Approvals, Tool-Pipeline, Model-Streaming, Status und Hook-Registrierung.
- Der Zustand steckt in vier Flags (`_closing`, `_canceling`, `_ready`, `_finishing`; `:115-118`) plus einem `status`-Literal und zwei Ambient-`ContextVar`s (`_preparing`, `_driving`).
- Fast jedes Plugin nimmt `Inject[Agent]` und liest `agent.session.events`. Der Agent ist damit de facto der Service-Locator.

**→ Empfehlung:**
- Aufteilen in `ModelStepper`, `ToolExecutor`, `ApprovalService` und `InboxService` als eigene Services im Context. Plugins injizieren genau das, was sie brauchen.
- Der Agent bleibt eine dünne Fassade.
- Zustand als `enum AgentState` plus Transitionstabelle. Unerlaubte Übergänge werfen an *einer* Stelle, statt dass jede Methode mit `if self._closing or self._canceling:` beginnt.

#### P1-2 · `RunResult` und `stream()` sind für Anwender zu roh
- `RunResult(text, events)` enthält weder Usage, Kosten, `stop_reason`, `turn_id` noch Structured Output.
- Ein **concurrent `run()`** liefert den letzten Assistant-Text des ganzen Idle-Intervalls statt des eigenen Turns.
- `stream()` liefert rohe Log-Events und endet erst, wenn der Driver idle ist.

**→ Empfehlung:**
- `RunResult(text, output, usage, stop_reason, turn_id, messages)`.
- `run()` und `stream()` werden an die eigene Inbox-ID bzw. den eigenen Turn gebunden.
- Die öffentliche Stream-Union steht in 3.2.

#### ~~P1-3 · DI-Reflection bei jedem Aufruf~~

**Resolved:** `BindingPlan` is compiled on registration. Listener and tool entries retain the plan; dynamic service values are never cached. Regression tests: `tests/test_di_plans.py`.
`bind_injected` (`kernel/di.py:38`) ruft bei jedem Listener-Aufruf, Tool-Call und Plugin-Start `get_type_hints` und `inspect.signature` auf. `ToolRegistry.register` berechnet `_Tool.injected` schon vorab, ruft danach aber trotzdem `bind_injected` auf.

**→ Empfehlung:** Bei der Registrierung einmal einen `BindingPlan` berechnen (Parametername → Key oder „vom Aufrufer“) und pro Callable cachen. Pro Aufruf bleibt dann nur ein Dict-Lookup im Context.

#### P1-4 · Drei Plugin-Autorenstile allein in den Built-ins
- Es gibt drei Stile:
  - einen Generator mit `@plugin` (`fs_local`, `persona`);
  - eine `Plugin`-Subklasse mit `@hook`/`@tool` (`Rules`, `SpillLargeResults`);
  - eine Factory-Funktion, die eine Instanz zurückgibt (`approval_gate`, `deepseek()`).
- `SpillLargeResults` und `LatestObservationOnly` rufen `super().__init__()` nicht auf und laufen nur dank Klassenattribut-Fallbacks.
- `Plugin.clone()` ist `copy.deepcopy(self)` (`kernel/declarations.py:95-96`). Das scheitert bei Clients, Locks und Closures.
- Plugins halten mutablen Zustand in `__init__` (`Rules._counts`, `LatestObservationOnly._previous`), der nur durch `clone` pro Mount getrennt wird.
- Die Overloads von `plugin()` geben `Callable[..., PluginSpec]` zurück (`kernel/plugin.py:273`). Dadurch gibt es keine IDE-Hilfe für `fs_local(root=...)`.
~~- `PluginSpec` ist frozen, enthält aber ein mutables `kwargs: dict`.~~

**→ Empfehlung:** Genau zwei Stile.
1. **Funktional:** `@plugin`-Generator mit `ParamSpec`-typisierter Config. `PluginSpec.kwargs` wird ein `MappingProxyType`.
2. **Klassenbasiert:**

   ```python
   class Spill(Plugin):
       class Config(BaseModel):
           threshold: PositiveInt = 32_000

       async def setup(self, ctx):
           self.store = ...  # per-mount state lives here only
   ```

   Die Klasse hat `@hook`/`@tool`-Methoden. Zustand entsteht nur in `setup()`, die Instanz ist reine Konfiguration. Damit entfallen `deepcopy` und `clone`.

Factory-Funktionen, die Instanzen zurückgeben, werden auf einen der beiden Stile umgestellt.

#### P1-5 · Ambient-Owner-Auflösung über `Context.current()`
`ToolRegistry.register` (`core/tools.py:78`), `SystemPrompt.section` (`core/prompt.py:34`), `Agent.on` und `add_hook` bestimmen den Owner über die contextvar. Ob ein Tool beim Dispose eines Plugins verschwindet, hängt also davon ab, *von wo* es registriert wurde. Dazu kommt die Sichtbarkeitsregel in `_binding` (`current._closed and handle.status == "stopping"`).

**→ Empfehlung:**
- Überall einen expliziten `owner: Context | None = None`-Parameter anbieten. Fehlt er, gilt die Ambient-Auflösung als dokumentierter Fallback.
- Im Debug-Modus wird geloggt, welchem Owner eine Registrierung zugeordnet wurde.
- Die Sichtbarkeitsregel bekommt einen eigenen Abschnitt in den Concepts-Docs (P2-8).

#### P1-6 · Presets: Duplikation und zwei Fehler
- Dieselben sieben Optionen stehen fünfmal da: `Harness.__init__`, `Harness.session`, `minimal`, `coding` und `headless`.
  **→ Empfehlung:** Die Optionen werden die Pydantic-Config des Loop-Plugins (`agent_loop(max_steps=..., tool_timeout=...)`, P1-8). Presets sind Funktionen, die einen `Agent` mit einer Plugin-Liste zurückgeben, und reichen keine Optionen mehr durch (3.1).
- **`headless` ist identisch mit `minimal`** (`presets.py:175-193`).
  **→ Empfehlung:** Entfernen.
~~- **`ctx.effect(agent.aclose)` wird zweimal registriert** (`presets.py:108`, `:111`).~~
  **Resolved:** Recipes are installed once through public `Context.install(..., settle=True)`; the agent has one registered disposer. Regression: `test_session_installs_waiting_plugins_once_and_disposes_agent_once`.
  **→ Empfehlung:** Eine Registrierung löschen.
~~- **Doppelte Installation:** Fehlt `AgentFactory` nach der ersten Runde, wird `ctx.install(pending)` aufgerufen (`:88`) und später noch einmal mit derselben Liste (`:110`). Der Pfad ist entweder tot oder mountet Plugins doppelt.~~
  **Resolved:** Recipes are installed once through public `Context.install(..., settle=True)`; the agent has one registered disposer. Regression: `test_session_installs_waiting_plugins_once_and_disposes_agent_once`.
  **→ Empfehlung:** `pending` genau einmal installieren und einen Test für den Fall „AgentFactory erscheint später“ ergänzen.
~~- Das Preset nutzt die private API `ctx._install_ready` (`:76`).~~
  **Resolved:** Recipes are installed once through public `Context.install(..., settle=True)`; the agent has one registered disposer. Regression: `test_session_installs_waiting_plugins_once_and_disposes_agent_once`.
  **→ Empfehlung:** Als `await ctx.install(..., settle=True)` öffentlich machen, denn eigene Harnesses brauchen das auch.
- `getattr(ctx[LLM], "model", "default")` ist Duck-Typing auf einem Protocol.
  **→ Empfehlung:** Aus `LLM.info.name` lesen (P0-6).

#### P1-7 · Events: Benennung und Registry
- Die `type`-Strings sind inkonsistent (`agent/inbox/inserted` gegen `InboxAdded`, `tools/result` gegen `ToolReturned`).
  **→ Empfehlung:** Den `type` aus dem Klassennamen ableiten (`"inbox.added"`). Die alten Strings bleiben als Aliase im Decoder, damit bestehende Logs lesbar bleiben.
- `ApprovalRequest` (Bail-Hook) und `ApprovalRequested` (Log-Event) unterscheiden sich nur im Suffix.
  **→ Empfehlung:** Hooks und Log-Events in getrennte Module legen (Abschnitt 5), Hooks im Imperativ (`RequestApproval`), Log-Events im Partizip (`ApprovalRequested`).
- `SESSION_EVENT_TYPES` wird von Hand gepflegt, und `encode_event`/`decode_event` dispatchen von Hand.
  **→ Empfehlung:** Registrierung per `__init_subclass__` plus Pydantic-Codec (P1-8). Jedes Event bekommt ein `version: ClassVar[int]` und optional `migrate()`.
- `StepEnd.reason` und `TurnEnd.reason` sind freie `str`.
  **→ Empfehlung:** `Literal`.

#### P1-8 · Validierungsstrategie: mehr Pydantic, aber gezielt
Heute steckt Pydantic nur in den Tool-Args und im DeepSeek-Wire. Überall sonst wird in `__post_init__` mit `type(x) is not int or x < 1` validiert, etwa in `RetryPolicy`, `ContextBudget` (`core/budget.py:32-36`), `ToolOutputLimit`, `ToolScheduling`, den Compaction-Policies (`plugins/context/compaction.py:27-36`), `SpillLargeResults` (`plugins/context/spill.py:17-21`), `when()` und den Events. Das sind zwei Systeme für dasselbe Problem.

**→ Empfehlung: Ja, deutlich mehr Pydantic, und zwar an allen Grenzen. Im Hot-Path dagegen nicht.**

| Wo | Was | Warum |
|---|---|---|
| **Plugin-Configs** | `class Config(BaseModel)` pro Plugin (P1-4) | Validierung, JSON-Schema, Doku und YAML/ENV-Konfiguration auf einen Schlag; ersetzt Cordis' `Schema` |
| **Options** | Loop-Config (`agent_loop`), `RetryPolicy`, `ContextBudget`, `ToolOutputLimit`, `Compaction`-Config als `BaseModel(frozen=True)` | Ersetzt die `__post_init__`-Checks; `PositiveInt`, `Field(ge=, le=)` und `Literal` sagen dasselbe deklarativ |
| **Log-Codec** | Ein `TypeAdapter` über die Discriminated Union aller Log-Events (`Field(discriminator="type")`) | Ersetzt den Hand-Dispatch in `encode_event`/`decode_event`; Migrationen bekommen einen festen Platz |
| **Built-in-Tool-Parameter** | `offset: Annotated[int, Field(ge=1)]`, `limit: Annotated[int, Field(le=16_000)]` | Die Grenze steht im JSON-Schema, das Modell *sieht* sie; heute validiert `read_file` von Hand (`plugins/tools/fs.py:23-24`) |
| **Provider-Wire** | wie heute im DeepSeek-Adapter | bleibt |
| **Nicht:** `Message`, `ContentPart`, `ToolCall`, Transient-Events, Projektions-State | `@dataclass(frozen=True, slots=True)` | Werden pro Token bzw. Step tausendfach erzeugt. Validiert werden sie beim Persistieren über den Codec, nicht beim Konstruieren. |

Regel für Contributor: *„Kommt der Wert von außen (User-Config, Datei, Netzwerk, Modell)? Dann Pydantic. Ist er intern erzeugt? Dann Dataclass.“* Diese Regel gehört in `CONTRIBUTING.md`.

#### P1-9 · Tool-System
- **Shadowing ohne Warnung:** Ein zweites Tool mit gleichem Namen verdeckt das erste (`core/tools.py:106-107`, `_visible` nimmt das letzte). Gedacht ist das für Overrides, es versteckt aber Bugs, etwa wenn zwei Plugins oder später zwei MCP-Server zufällig ein `search` mitbringen.
  **→ Empfehlung: Standardmäßig failen.**
  - `register` wirft `ToolNameConflict` mit beiden Ownern in der Meldung. Der Fehler schlägt im `setup` des Plugins auf, wird nach P0-4 sofort geloggt, und das Plugin startet nicht.
  - Gewollte Overrides deklarieren das mit `@tool(override=True)` bzw. `register(..., override=True)`.
  - Kein Warning als Zwischenstufe, denn Warnings gehen im Log unter. Vor 1.0 kostet ein harter Fehler nichts, und ein falsches Tool ist eine stille Fehlfunktion, die sich zur Laufzeit kaum findet.
  - Die Owner-Logik bleibt erhalten: Wird das überschreibende Plugin disposed, ist das ursprüngliche Tool wieder sichtbar.
  - Für MCP gibt es optional ein Präfix (`mcp(server, prefix="github")` → `github.search`), damit Konflikte gar nicht erst entstehen.
- **Drei `tool`-Decorators mit kopierten Overloads** (`ToolRegistry.tool`, `features.tool`, `Bundle.tool`) und zwei Metadaten-Attribute (`_harnessed_tool`, `_harnessed_registrations`).
  **→ Empfehlung:** Ein `harnessed.tool`, der ein `ToolSpec` an die Funktion hängt. Registry und Bundle konsumieren dieses Spec.
- **Sync-Tools laufen in Threads und sind nicht abbrechbar.** `asyncio.timeout` lässt den Thread weiterlaufen.
  **→ Empfehlung:** Das dokumentieren. `ToolCallContext.signal` prominent machen und optional `@tool(isolate="process")` für CPU-lastige oder hängende Tools anbieten.
- **Shell-Sonderfall im Core:** `core/tools.py:189-194` kennt `ProcResult`.
  **→ Empfehlung:** Ein Result-Converter-Registry (`ToolRegistry.convert(ProcResult, fn)`), das das Shell-Plugin befüllt.
- **Zirkuläre Imports** (`from .loop import Agent` in `core/tools.py:121`, `:172`).
  **→ Empfehlung:** `ToolCallContext` kennt nur `session`, `inbox` und `signal`, nicht den Agent. Damit verschwindet der Zyklus.
~~- **Redundanz:** `tool.signature.bind(...)` (`:177`) nach `bind_injected` sowie die Unknown-Argument-Schleife in `ToolArguments.parse` (`core/validation.py:70-72`) neben `extra="forbid"`.~~
  **→ Empfehlung:** Beides löschen.
~~- `ToolArguments.schema` ruft bei jedem Step für jedes Tool `model_json_schema` auf.~~
  **→ Empfehlung:** Mit `functools.cached_property` cachen.

#### P1-10 · DeepSeek-Adapter
- **Neuer `httpx.AsyncClient` pro Request,** wenn kein `client` übergeben wird (`adapters/deepseek.py:121`). Ohne `aclose`.
  **→ Empfehlung:** Ein Client pro Adapter-Instanz, lazy erzeugt und über `aclose()` bzw. den Plugin-Disposer geschlossen.
- **Der Fehlerbody wird nicht gelesen** (`raise_for_status()` im Stream, `:130`).
  **→ Empfehlung:** Vorher `await response.aread()` und den Body in die Exception packen.
- **Keine typisierte Fehlerhierarchie.**
  **→ Empfehlung:** `ProviderError` mit den Unterklassen `RateLimited(retry_after)`, `ContextLengthExceeded`, `AuthError` und `InvalidRequest` in `seams`. Damit wird reaktive Compaction möglich (4.2).
- **`Retry-After` wird ignoriert, Retries laufen ohne Jitter** (`adapters/base.py:34-50`).
  **→ Empfehlung:** Exponentielles Backoff mit Full-Jitter, wobei `Retry-After` Vorrang hat.
- Der Default-Modellname `"deepseek-flash"` ist hart kodiert (`:63`).
  **→ Empfehlung:** Den Modellnamen als Pflichtparameter verlangen.
- **Strategisch** ist DeepSeek der einzige Provider.
  **→ Empfehlung:** Ein generischer **OpenAI-kompatibler** Adapter als Basis (deckt DeepSeek, vLLM, Ollama, OpenRouter und Together ab), DeepSeek als dünne Subklasse, dazu ein Anthropic-Adapter als Extra.

#### P1-11 · Prompt-Sektionen
- Synchrone Section-Callables laufen per `asyncio.to_thread` (`core/prompt.py:62`), obwohl sie meist triviale String-Funktionen sind.
  **→ Empfehlung:** Synchron direkt aufrufen. Wer I/O braucht, schreibt die Section als `async def`.
- `stable=True` cached **für immer**.
  **→ Empfehlung:** Ein `SystemPrompt.invalidate(name)` und optional `stable_for=timedelta(...)`.
- Der Name ist der Schlüssel, ein zweites `persona(...)` ersetzt das erste still.
  **→ Empfehlung:** Wie bei Tools (P1-9): Ersetzen nur mit `override=True`, sonst ein Fehler.

#### P1-12 · `Rules` blähen das Log auf
`Rules.review` hängt für **jede Regel × jedes Tool-Result** ein `RuleEvaluated` an (`plugins/permissions/rules.py:126`). Bei 10 Regeln und 500 Tool-Calls sind das 5000 Events für reine Zähler.

**→ Empfehlung:** Persistiert wird nur, wenn eine Regel auslöst (`RuleTriggered`). Die Zählerstände sind eine Projektion über `ToolReturned` und müssen nicht separat gespeichert werden.

---

### P2: Konventionen, Organisation, Feinschliff

- **P2-1 · Typing-Tiefe.** `ty --error-on-warning` läuft in CI, das ist gut. Ob die *öffentliche* API so typisiert ist, wie Nutzer sie erleben, prüft aber noch nichts (z. B. ParamSpec-Verlust in P1-4). Dazu kommt `PluginHandle.status` (`kernel/plugin.py:91`) als `str` statt `Literal`.
  **→ Empfehlung:**
  - Ein `tests/typing/`-Ordner mit `assert_type`-Fällen für die wichtigsten Signaturen (`tool`, `plugin`, `Inject`, `run`), den ty mitprüft.
  - Statusfelder als `Literal` oder `Enum`.
- **P2-2 · Lokale Imports als Zyklus-Workaround** (`presets.py:67-69`, `129-130`, `154-158`; `plugins/permissions/approval.py:49`; `kernel/plugin.py`).
  **→ Empfehlung:** Zyklen über die Aufteilung in P1-1 und P1-9 auflösen. Danach verbietet Ruff mit `PLC0415` (import-outside-toplevel) neue lokale Imports.
- **P2-3 · Kleinkram im Kernel:**
~~  - `self.listeners.insert(0, ...) if prepend else self.listeners.append(...)` steht als Ausdruck in Statement-Position (`kernel/events.py`).~~
    **→ Empfehlung:** Ein normales `if`.
  - Der Kwarg `_scope` ist privat, steht aber im öffentlichen Konstruktor (`kernel/context.py:48`).
    **→ Empfehlung:** Eine Klassenmethode `Context._child(...)`.
  - `setup` läuft außerhalb des `try` (`kernel/declarations.py`), sodass `teardown` nach einem fehlgeschlagenen `setup` nicht läuft und halb allokierte Ressourcen leaken.
    **→ Empfehlung:** `contextlib`-Semantik: Ressourcen über `ctx.effect(...)` registrieren, die auch bei einem Fehler in `setup` abgebaut werden.
  - Prioritäten sind nackte Zahlen (`approval_gate` mit `priority=-100`), und „niedriger läuft zuerst“ steht nur im Code.
    **→ Empfehlung:** Benannte Konstanten (`Priority.GUARD`, `Priority.DEFAULT`, `Priority.OBSERVE`) plus Docstring an `hook`.
- **P2-4 · Hashbarkeit.** `freeze()` erzeugt `MappingProxyType`. Frozen Dataclasses mit solchen Feldern sind dadurch nicht hashbar, obwohl `frozen=True` das suggeriert.
  **→ Empfehlung:** `eq=True, unsafe_hash=False` bewusst setzen und dokumentieren, oder `frozendict`-artige Tupel von Paaren verwenden, wo Hashbarkeit gebraucht wird.
- **P2-5 · `Message.role` als String statt getaggter Union.** Invarianten wie „nur Assistant hat `tool_calls`“ prüft die Runtime statt des Typsystems.
  **→ Empfehlung:** `UserMessage | AssistantMessage | ToolMessage | SystemMessage` als Dataclass-Union. Das passt auch zum Pydantic-Codec (P1-8).
- **P2-6 · `harnessed.testing` ist zu dünn.** Es exportiert nur `ScriptedLLM`.
  **→ Empfehlung:** Ein Paket `harnessed.testing` mit:
  - `FakeShell`, `InMemoryFS` und `InMemoryPersistence`;
  - Assertions wie `assert_tool_called(session, "read_file", path=...)`;
  - einem pytest-Plugin (`harnessed_agent`-Fixture).

  Der `ExtractiveCompactor` zieht ebenfalls hierher (4.2).
- **P2-7 · Modulnamen.** `plugins/observe/tokens.py` beobachtet nichts, und `adapters/__init__.py` ist leer.
  **→ Empfehlung:** Mit der Zielstruktur in Abschnitt 5 umbenennen. Token-Counter liefert der Adapter (P0-6).
- **P2-8 · Doku: README kompakt, Rest nach `/docs`.** Das README hat 1548 Zeilen, die Inbox-Tabelle steht doppelt, und Context, Plugin, Bundle, Harness, Inject, needs und Waterfall kommen alle vor dem ersten nützlichen Agent.
  **→ Empfehlung:** Ja, der Vorschlag ist richtig.
  - **README (150–200 Zeilen):** Pitch in drei Sätzen, Installation, das Hello World aus 3.1, ein Stream-Beispiel, eine Feature-Liste mit Links und Links auf die Docs.
  - **`/docs`** mit mkdocs-material plus mkdocstrings:
    ```
    docs/
      index.md                 # what & why, comparison to other SDKs
      quickstart.md            # 5 minutes to a working agent
      guides/
        tools.md               # @tool, validation, readonly/side_effects, parallelism
        sessions.md            # persistence, resume, fork
        streaming.md           # StreamEvent, UIs
        inbox-and-control.md   # followup/steer/inject, pause/resume/stop
        approvals.md
        context-management.md  # output limit, spill, compaction
        providers.md           # openai-compatible, deepseek, anthropic, custom adapters
        observability.md       # logging, tracing, otel
        testing.md
        plugins.md             # the two authoring styles
      concepts/
        kernel.md              # Context, services, needs, effects, ownership
        events.md              # emit/bail/waterfall/parallel, priorities
        session-log.md         # log, projections, durability modes
      reference/               # generated by mkdocstrings from the docstrings
      changelog.md
    ```
  - Code in den Docs wird aus `examples/` eingebunden (`--8<--` Snippets). Die Beispiele laufen schon in CI (`tests/test_examples.py`), dadurch veralten die Docs nicht.
  - Der Docs-Build mit `mkdocs build --strict` läuft in CI.
- **P2-9 · Beispiele.** `01_tool_agent.py` baut den Harness von Hand mit `agent_loop()` und `llm()`.
  **→ Empfehlung:** Beispiel 01 zeigt exakt das Hello World aus 3.1. Beispiel 02 zeigt dieselbe App in der aufgelösten Form `Agent(plugins=[...])`, damit der Schritt zu eigenen Plugins sichtbar wird. Kernel-Ebene (`12_layered_sdk.py`) bleibt, aber als eines der letzten Beispiele.

---

## 3. API-Ergonomie: Zielbild

### 3.1 Hello World

**Heute** (Beispiel 01, gekürzt):

```python
from harnessed import Context, Harness, ModelChunk, ToolCall, ToolRegistry, plugin
from harnessed.core.events import AssistantChunk, ToolCalled, ToolReturned
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.context.instructions import persona
from harnessed.plugins.services import llm


@plugin(needs=[ToolRegistry])
async def calculator(ctx: Context):
    @ctx[ToolRegistry].tool
    async def add(a: int, b: int) -> int: ...

    yield


harness = Harness([agent_loop(), llm(model), calculator(), persona("...")])
async with harness.session("calculator") as agent:
    ...
```

**Ziel:**

```python
from harnessed import Agent, Plugin, tool
from harnessed.llms import openai_compatible  # or: deepseek, anthropic


class Calculator(Plugin):
    @tool(readonly=True)
    async def add(self, a: int, b: int) -> int:
        """Add two integers."""
        return a + b


agent = Agent(
    llm=openai_compatible(model="deepseek-chat"),
    instructions="Keep answers concise.",
    plugins=[Calculator()],
)

async with agent.session("calc") as session:
    result = await session.run("What is 19 + 23?")
    print(result.text, result.usage.total_tokens, result.stop_reason)
```

**Benennung `llm` statt `model`:** „Model“ ist in einem Pydantic-lastigen Paket mehrdeutig, denn es gibt schon `seams/models.py` und `adapters/deepseek_models.py` für Datenmodelle. Deshalb gilt durchgängig: **`llm`** ist das Client-Objekt (passend zum `LLM`-Protocol), **`model`** ist nur der Modellname als String. Das Modul heißt `harnessed.llms`, der Parameter `Agent(llm=...)`.

Ein Import für den Normalfall. Ein Plugin, das nur Tools enthält, ist die einfachste Plugin-Form und braucht weder Context noch Hooks. Hooks und Inject sind die *zweite* Ebene, für alle, die das Verhalten des Loops ändern wollen.

**Die Fassade ist Zucker über Plugins, kein zweites System.** Damit bleibt das Cordis/DSH-Prinzip „everything is a plugin“ erhalten:
1. **`Agent` hat genau drei Parameter:** `llm`, `instructions` und `plugins`. Alles andere ist ein Plugin in `plugins=[...]`, also Tools, Persistenz, Approvals, Compaction, Spill, MCP und Services (`provide(T, obj)`). Es gibt keine Parameter wie `tools=`, `compaction=` oder `services=`.
2. **Tools leben immer in Plugins**, als `@tool`-Methoden einer `Plugin`-Klasse (oder `@ctx.tool` im funktionalen Stil). Lose Funktionen als Tools gibt es nicht. Ein Plugin bündelt so zusammengehörige Tools mit ihrem Zustand, ihrer Config und ihrem Lebenszyklus, und es gibt genau einen Registrierungsweg. Ein eigenes `Toolset`-Konzept ist damit überflüssig.
3. **Jeder Parameter entspricht 1:1 einem öffentlichen Plugin.** Ein Test sichert ab, dass `Agent(llm=x, instructions=s)` exakt `Agent(plugins=[llm(x), instructions(s)])` ergibt.
4. **Defaults sind normale Plugins mit Slot:** `agent_loop`, `memory_sessions`, `ToolOutputLimit`, `Approvals()` und der No-op-Tracer. Ein Plugin mit demselben Slot ersetzt den Default. Ein doppelter Slot in `plugins=` ist ein `PluginConflict`.
5. **Built-ins nutzen nur öffentliche API** (vgl. `ctx._install_ready` in P1-6).
6. **`agent.plugins` zeigt die aufgelöste Zusammensetzung.** Auch Presets sind damit keine Black Box.

Das vollständige Zielbild aller Fälle steht in [`api-target.md`](api-target.md).

### 3.2 Typisierter Stream

```python
async for event in session.stream("Refactor utils.py"):
    match event:
        case TextDelta(text=t):
            print(t, end="")
        case ToolStarted(call=c):
            ui.spinner(c.name)
        case ToolFinished(call=c, result=r):
            ui.done(c.name, r.status)
        case ApprovalNeeded(request=req):
            await req.answer(ask_user(req))
        case TurnFinished(result=res):
            break
```

- `StreamEvent` ist eine kleine, stabile Union mit 6–8 Typen. `TextDelta` ist das transiente Event aus P0-2.
- Rohe Log-Events gibt es weiter über `session.events` bzw. `session.subscribe()`.
- Der Stream endet mit dem *eigenen* Turn (P1-2).

### 3.3 Structured Output

```python
class Plan(BaseModel):
    steps: list[str]


result = await session.run("Plan the migration", output=Plan)
result.output.steps  # typed
```

Umgesetzt über `ModelSettings.response_format` (P0-6). Kann der Provider kein `response_format`, greift der Fallback auf das „final_answer“-Tool-Pattern. Bei einem Validierungsfehler wird einmal mit der Fehlermeldung nachgefragt.

### 3.4 Approvals

```python
agent = Agent(
    ...,
    plugins=[
        Filesystem(root="."),  # read_file (readonly), write_file, edit
        Shell(),               # run_command
        Approvals(
            approver=cli_approver(),  # or a callback / web UI
            trust=["read_file"],      # everything not readonly/trusted -> ASK
        ),
    ],
)
```

Defaults und `PolicyDecision`/`ApprovalAnswer` stehen in P0-5 und P0-6.

### 3.5 Zielstruktur der Exports

| Modul | Inhalt |
|---|---|
| `harnessed` | `Agent`, `Session`, `RunResult`, `tool`, `ToolError`, `ToolContext`, `Plugin`, `plugin`, `hook`, `Inject`, `Message`, `ToolResult`, `StreamEvent`-Typen (etwa 20 Namen) |
| `harnessed.llms` | LLM-Adapter: `openai_compatible`, `deepseek`, `anthropic` (Extra), Basisklasse `StreamingLLM` |
| `harnessed.events` | Log-Events (persistierbar) |
| `harnessed.hooks` | Hook-Events (`PrepareRequest`, `ToolPolicy`, …) |
| `harnessed.plugins` | **Alle** mitgelieferten Plugins an einem Ort: Defaults (`agent_loop`, `memory_sessions`, `ToolOutputLimit`, `Approvals`) und Opt-ins (`jsonl_sessions`, `Compaction`, `Spill`, `Rules`, `LatestObservationOnly`, `provide`) |
| `harnessed.kernel` | `Context`, `Inject`, `Key`, `needs`, Effects (für Framework-Autoren) |
| `harnessed.seams` | Protocols für eigene Adapter, `ProviderError`-Hierarchie |
| `harnessed.integrations` | `mcp` (Extra), später `otel`; beides Plugins |
| `harnessed.testing` | `ScriptedLLM`, Fakes, Fixtures, Assertions |

---

## 4. Scope: Was soll Harnessed mitliefern und was nicht?

### 4.1 Leitprinzip

> **Mechanismen in den Core, Policies als Opt-in, Adapter als Extras, Spielzeug nach `testing`.**

Quer dazu gilt: **Alles Optionale ist ein Plugin**, und Built-ins haben keinen privilegierten Weg (3.1).

1. **Core (immer da):** alles, was **Log-Format, Projektionen oder Durability** berührt. Das kann ein Nutzer von außen nicht korrekt nachbauen.
2. **Built-in Policy (mitgeliefert, opt-in, *eine* gute Variante):** Entscheidungslogik, die fast jeder braucht und die man leicht falsch macht.
3. **Extras (`harnessed[anthropic]`, `harnessed[mcp]`, `harnessed[otel]`):** Integrationen mit fremden Dependencies.
4. **Rezepte in `/docs`, nicht im Paket:** DSH-Heuristiken, die eine *Meinung* über Agent-Verhalten sind und keine Infrastruktur.
5. **Nicht mitliefern:** Spielzeug-Defaults, die produktionsreif *aussehen*. Sie sind schlimmer als gar keine.

### 4.2 Compaction

**Ja, eine Default-Implementierung offenlegen, aber eine andere als heute.**

| Teil | Heute | → Empfehlung | Begründung |
|---|---|---|---|
| `CompactionApplied`, Projektion, `plan_compaction`, `compact_request` | `core/compaction.py` | **Core, so lassen** | Berührt die Message-Projektion. |
| Trigger-Policies | `Threshold`, `SlidingWindow`, `Periodic` plus `ContextBudgetGuard`, die sich nur in `_due` unterscheiden | **Eine** Klasse `Compaction(trigger=at_ratio(0.8), keep_turns=2, summarizer=...)`. Trigger sind Callables (`at_ratio`, `every_turns`), das Budget wird Teil davon. | Weniger API, gleiche Mächtigkeit. |
| Summarizer-Default | `ExtractiveCompactor`, implizit in **jedem** Harness (`plugins/context/providers.py:11`, `presets.py:81`) | **`LLMCompactor` mit dem LLM der Session** (optional ein günstigeres Modell). `ExtractiveCompactor` kommt nach `harnessed.testing` (P2-6). Nichts wird implizit installiert. | Ein Extractive-„Summary“ *wirkt* wie Compaction, verliert aber Entscheidungen. DSH, Claude Code und Codex fassen alle per LLM zusammen. |
| Summarizer-Prompt | hart kodiert (`adapters/compaction.py:52-56`) | `Compaction(instructions=...)` mit gutem Default | Coding- und Support-Agents brauchen andere Summaries. |
| Preset | nirgends aktiv | **`coding`: an** (`at_ratio(0.8)`). **`minimal`: aus**, aber `ContextBudgetExceeded` nennt in der Fehlermeldung konkret `Compaction(...)`. | Ein Coding-Agent, der bei vollem Kontext crasht, ist schlechter als ein sichtbarer, überschreibbarer Default. |
| Hook-Punkt | Compaction hängt in `PrepareRequest` Events an und setzt `cache_breakpoints=()` (`plugins/context/compaction.py:86-93`). Das Ergebnis hängt von der Hook-Reihenfolge ab. | Eigener Hook `BeforeStep`, *vor* dem Request-Bau. `PrepareRequest` bleibt eine reine Transformation. | Seiteneffekte gehören nicht in einen Waterfall. |
| Wirkungslose Compaction | Bei `after >= before` wird das Ergebnis still verworfen (`:89-91`), der Summarizer-Call ist trotzdem bezahlt. | `WARNING` loggen (P0-4) und einen Backoff setzen, damit nicht in jedem Step erneut compacted wird. | Sonst gibt es eine teure Endlosschleife. |
| Reaktive Compaction | fehlt | Bei `ContextLengthExceeded` (P1-10) einmal compacten und den Request wiederholen. | So machen es die ausgereiften Harnesses. |

### 4.3 Die anderen Komponenten

| Komponente | → Empfehlung | Begründung |
|---|---|---|
| Session-Log, Persistenz, Resume, Fork | **Core plus JSONL- und In-Memory-Adapter**, SQLite/Postgres später als Extra | Alleinstellungsmerkmal. Async-Writer nach P0-3. |
| Inbox, pause/resume/stop | **Core** | Durability-relevant und gut umgesetzt. |
| Approvals plus ALWAYS-Grants | **Core-Mechanismus**, `Approvals(...)` als Built-in-Policy, CLI-Approver in `/docs` als Rezept | Defaults nach P0-5. |
| `ToolOutputLimit` | **Core, per Default an** | Einfach, immer nötig, gut umgesetzt. |
| Spill | **Built-in `Spill`, in `coding` an.** Der Store wird an die Session gebunden (Unterordner der Persistence, gelöscht mit der Session) und bekommt `max_age`/`max_bytes`. | Heute schreibt `LocalSpillStore` nach `./.harnessed/spills` relativ zum CWD und räumt nie auf. |
| `CacheBreakpoints`, `CacheMetrics` | **Entfernen** | No-op (1.2). |
| `Rules` / `when()` | **Opt-in in `harnessed.plugins`**, nur noch `RuleTriggered` persistieren (P1-12) | DSH-Heuristik, eine Meinung, keine Infrastruktur. |
| Observations, `LatestObservationOnly` | **Opt-in in `harnessed.plugins`.** Der Core behält `ObservationRecorded`. | Nischig. |
| `persona`, `agents_md` | `instructions=` als Agent-Parameter; `agents_md` im `coding`-Preset | Ein Plugin für einen String ist zu viel Zeremonie. |
| FS- und Shell-Tools, `coding`-Preset | **Entweder richtig oder als Beispiel.** Richtig heißt: `list_dir`, `glob`, `grep`, `edit`, eine gehärtete Shell (P0-5) und `ASK` als Default. | Die Preset-Qualität ist der erste Eindruck. |
| Token-Counting, Kontextfenster | Approximation als Fallback, ansonsten liefert der Adapter Counter und `ModelInfo` (P0-6) | 128k global ist für die meisten Modelle falsch. |
| Provider | **OpenAI-kompatibel als Basis, DeepSeek als Subklasse, Anthropic als Extra** (P1-10) | Ein Provider ist zu wenig. |
| MCP-Client | **Extra `harnessed[mcp]`.** MCP-Tools werden normale Tools. | Ohne MCP keine Tool-Ökosysteme. |
| Subagents / Agent-as-Tool | **Core-Mechanismus:** eine Kind-Session als eigenes Log mit Parent-Referenz, das Ergebnis als `ToolResult` | Das meistgenutzte Muster moderner Agents. Das Log-Design eignet sich ideal dafür. |
| Structured Output | **Core** (3.3) | |
| Tracing | **Seam im Core jetzt, OTel-Exporter später als Extra** (P0-4) | Der Seam verhindert einen späteren Breaking Change, der Exporter ist eine Erweiterung. |
| Testing-Utilities | **Mitliefern** (P2-6) | |
| Memory / RAG, UI/TUI, Evals | **Nicht mitliefern** | Eigene Domänen. Hooks reichen, Integrationen leben in eigenen Paketen. |

**Ergebnis:** Das Paket wird nicht größer, sondern schärfer.
- **Raus:** `CacheBreakpoints`, `CacheMetrics`, der implizite `ExtractiveCompactor`, 3 von 4 Compaction-Klassen, `headless`, `AgentHandle` sowie Rules und Observations aus dem Top-Level.
- **Rein:** Structured Output, typisierter Stream, Subagents, MCP, der OpenAI-kompatible Adapter, Logging und der Tracer-Seam. OTel folgt später.

---

## 5. Code-Organisation

**Gut und beizubehalten:**
- Die Schichtung kernel ← seams ← core ← plugins/adapters ← presets wird per `tests/test_architecture.py` erzwungen.
- Die Projektionen (`inbox.py`, `turns.py`, `control.py`, `approvals.py`) sind reine Funktionen auf dem Log. Das ist die ideale Ausgangslage für die Reducer aus P0-1.

**Zu ändern:**
- `core/events.py` hat 494 Zeilen und mischt Hook-Events (mit Agent-Referenz, nicht serialisierbar) und persistierbare Log-Events.
  **→ Empfehlung:** Aufteilen in `core/hooks.py` und `core/events.py`. Der Architekturtest stellt sicher, dass Log-Events keine Agent- oder Context-Referenzen enthalten.
- Die Grenze zwischen Core und Plugins ist unscharf. `ToolRegistry` und `SystemPrompt` liegen in `core`, werden aber über `plugins/services.py` bereitgestellt, die der Harness *immer* installiert.
  **→ Empfehlung:** Das Loop-Plugin stellt beide bereit, weil es sie braucht. `plugins/services.py` entfällt, und der Default-Satz aus 3.1 bleibt klein und explizit.
- Namenskollisionen und Agent-Aufteilung stehen in P0-7 und P1-1.

**Zielstruktur:**

```
harnessed/
  __init__.py          # ~20 names
  kernel/              # unchanged, plus optional inject and config schema
  seams/               # broader: multimodal ToolResult, stop_reason, settings, ProviderError
  core/
    agent.py           # thin facade
    runtime/           # stepper.py, tool_executor.py, state.py (state machine)
    session/           # log.py, writer.py, projections/ (incremental), codec.py (Pydantic union)
    hooks.py           # hook events
    events.py          # log events (persistable)
    stream.py          # StreamEvent union (transient)
    compaction.py, approvals.py, inbox.py, ...
  llms/                # _base.py (StreamingLLM), openai_compatible, deepseek, anthropic
  plugins/             # opt-in policies
  integrations/        # mcp (extra), otel later
  testing/             # fakes, fixtures, assertions, pytest plugin
```

---

## 6. Roadmap

**Phase 1: Fundament vor dem API-Freeze (Breaking erlaubt)**
1. Seams erweitern (P0-6).
2. Chunks transient statt persistiert (P0-2), inkrementelle Projektionen (P0-1), Async-Writer (P0-3).
3. Logging und Tracer-Seam (P0-4). Der OTel-Exporter kommt erst in Phase 3.
4. Sichere Defaults, Shell-Härtung, Error-Formatter (P0-5).
5. Exports schneiden und Kollisionen auflösen (P0-7).

**Phase 2: Ergonomie**
6. `Agent`-Fassade als Zucker über Plugins und `@tool`-Methoden in Plugins ohne Registry-Zeremonie (3.1), typisierter Stream und reiches `RunResult` (P1-2, 3.2), Structured Output (3.3).
7. Pydantic an den Grenzen (P1-8), zwei Plugin-Stile (P1-4), DI-Binding-Plan (P1-3).
8. Agent aufteilen, State-Machine (P1-1), Events neu ordnen (P1-7, Abschnitt 5).
9. README kompakt, `/docs` aufbauen (P2-8), Beispiele anpassen (P2-9).

**Phase 3: Feature-Parität mit modernen Agents**
10. OpenAI-kompatibler Basis-Adapter, Anthropic, typisierte Provider-Fehler (P1-10).
11. MCP (Extra) und Subagents (4.3).
12. Compaction-Umbau inklusive reaktiver Compaction (4.2).
13. `coding`-Preset vollwertig machen oder zum Beispiel degradieren (4.3).
14. `harnessed.testing` ausbauen (P2-6).
15. OTel-Exporter als Extra `harnessed[otel]` (P0-4).

---

## 7. Was unbedingt bleiben soll

Damit das strenge Urteil nicht falsch ankommt: Diese Dinge macht Harnessed besser als die meisten Agent-SDKs, und sie müssen jeden Umbau überleben.

- **Everything is a plugin.** Auch Loop, LLM und Persistenz sind austauschbare Plugins. Die neue Fassade ändert daran nichts (3.1).
- **Das append-only Log als Single Source of Truth** mit Fork, Resume, Crash-Repair und Tail-Recovery.
- **Durable Inbox und Run-Control.** Steer und Inject während eines laufenden Turns sind ein echtes Differenzierungsmerkmal.
- **Temporale Plugin-Lebenszyklen mit reversiblen Effects.** Hot-Swapping von Services ist sauber gelöst.
- **Event-Semantik am Typ** (Emit, Bail, Waterfall, Parallel).
- **Parallele Tool-Ausführung mit Ressourcen-Keys** (`core/scheduling.py`), korrekt in der Commit-Reihenfolge.
- **Strikte Tool-Argument-Validierung mit Pydantic**, das Schema wird schon bei der Registrierung geprüft.
- **Architekturtest, CI-Matrix und Testdisziplin:** 431 Tests in 17 Sekunden, über drei Betriebssysteme und drei Python-Versionen, Beispiele inklusive.

Das Fundament stimmt. Die Arbeit liegt jetzt an der Oberfläche, in der Breite der Seams und im Laufzeitverhalten.
