"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from "./compare-table";
import { MAX_COMPARE, compareHref } from "@/lib/url";
import type { LLMRow, STTRow, TTSRow } from "@/lib/types";

type Props = { llm: LLMRow[]; stt: STTRow[]; tts: TTSRow[] };

function parseIds(raw: string | null, knownIds: Set<string>): string[] {
  const filtered = (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .filter((id) => knownIds.has(id));
  return [...new Set(filtered)].slice(0, MAX_COMPARE);
}

export function CompareModels({ llm, stt, tts }: Props) {
  const searchParams = useSearchParams();
  // Deep-linked ?m= ids are validated against the real catalog before they
  // ever reach state, so a garbage id can never occupy one of the
  // MAX_COMPARE slots and silently block later adds.
  const knownIds = useMemo(
    () =>
      new Set([...llm, ...stt, ...tts].map((m) => m.model_id)),
    [llm, stt, tts],
  );
  const [ids, setIds] = useState<string[]>(() =>
    parseIds(searchParams.get("m"), knownIds),
  );

  // Keep the URL shareable without triggering a navigation or a server render.
  useEffect(() => {
    const href = compareHref(ids);
    if (window.location.pathname + window.location.search !== href) {
      window.history.replaceState(null, "", href);
    }
  }, [ids]);

  const add = useCallback((id: string) => {
    setIds((prev) =>
      prev.includes(id) || prev.length >= MAX_COMPARE ? prev : [...prev, id],
    );
  }, []);

  const clear = useCallback(() => setIds([]), []);

  const selectedLLM = ids
    .map((id) => llm.find((m) => m.model_id === id))
    .filter(Boolean) as LLMRow[];
  const selectedSTT = ids
    .map((id) => stt.find((m) => m.model_id === id))
    .filter(Boolean) as STTRow[];
  const selectedTTS = ids
    .map((id) => tts.find((m) => m.model_id === id))
    .filter(Boolean) as TTSRow[];
  const total = selectedLLM.length + selectedSTT.length + selectedTTS.length;
  const currentIds = [...selectedLLM, ...selectedSTT, ...selectedTTS].map(
    (m) => m.model_id,
  );

  return (
    <>
      <div className="wrap" style={{ padding: "20px 0" }}>
        <aside className="filed-panel">
          <b>COMPARE</b>
          <span>Up to {MAX_COMPARE} models, side-by-side.</span>
          <dl>
            <dt>SELECTED</dt>
            <dd>{total}</dd>
            <dt>LLM</dt>
            <dd>{selectedLLM.length}</dd>
            <dt>STT</dt>
            <dd>{selectedSTT.length}</dd>
            <dt>TTS</dt>
            <dd>{selectedTTS.length}</dd>
          </dl>
        </aside>
      </div>

      <div className="toolbar">
        <div className="wrap row">
          <a href="/costs" className="btn btn-outline">
            ← all costs
          </a>
          {total > 0 && (
            <button type="button" className="btn btn-outline" onClick={clear}>
              clear
            </button>
          )}
          <div
            style={{
              marginLeft: "auto",
              display: "flex",
              alignItems: "center",
              gap: 12,
            }}
          >
            <div className="anchors">
              {selectedLLM.length > 0 && <a href="#cmp-llm">LLM</a>}
              {selectedSTT.length > 0 && <a href="#cmp-stt">STT</a>}
              {selectedTTS.length > 0 && <a href="#cmp-tts">TTS</a>}
            </div>
            <div
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 11,
              }}
            >
              {total} of {MAX_COMPARE} slots
            </div>
          </div>
        </div>
      </div>

      <section className="cat">
        <div className="wrap">
          {selectedLLM.length > 0 && (
            <div id="cmp-llm">
              <LLMCompareTable models={selectedLLM} currentIds={currentIds} />
            </div>
          )}
          {selectedSTT.length > 0 && (
            <div id="cmp-stt">
              <STTCompareTable models={selectedSTT} currentIds={currentIds} />
            </div>
          )}
          {selectedTTS.length > 0 && (
            <div id="cmp-tts">
              <TTSCompareTable models={selectedTTS} currentIds={currentIds} />
            </div>
          )}

          <h3
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              fontWeight: 700,
              letterSpacing: "0.14em",
              textTransform: "uppercase",
              color: "var(--color-mute)",
              margin: "24px 0 16px",
            }}
          >
            {total === 0 ? "Available models" : "Add another model"}
          </h3>
          <ModelGroup
            label="LLMs"
            models={llm}
            currentIds={currentIds}
            onAdd={add}
          />
          <ModelGroup
            label="Speech-to-Text"
            models={stt}
            currentIds={currentIds}
            onAdd={add}
          />
          <ModelGroup
            label="Text-to-Speech"
            models={tts}
            currentIds={currentIds}
            onAdd={add}
          />
        </div>
      </section>
    </>
  );
}

function ModelGroup({
  label,
  models,
  currentIds,
  onAdd,
}: {
  label: string;
  models: { provider: string; display_name: string; model_id: string }[];
  currentIds: string[];
  onAdd: (id: string) => void;
}) {
  const available = models.filter((m) => !currentIds.includes(m.model_id));
  if (available.length === 0) return null;
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: "0.12em",
          textTransform: "uppercase",
          color: "var(--color-mute)",
          marginBottom: 8,
        }}
      >
        {label}
      </div>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {available.map((m) => (
          <button
            key={m.model_id}
            type="button"
            className="add-pill"
            disabled={currentIds.length >= MAX_COMPARE}
            style={{
              opacity: currentIds.length >= MAX_COMPARE ? 0.4 : undefined,
              cursor:
                currentIds.length >= MAX_COMPARE ? "not-allowed" : undefined,
            }}
            onClick={() => onAdd(m.model_id)}
          >
            <span className="add-pill-plus">+</span>
            <span>
              <span className="add-pill-prov">{m.provider}</span>{" "}
              {m.display_name}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
