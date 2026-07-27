"use client";

import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  LLMCompareTable,
  STTCompareTable,
  TTSCompareTable,
} from "./compare-table";
import { MAX_COMPARE, compareHref } from "@/lib/url";
import type { LLMRow, STTRow, TTSRow } from "@/lib/types";

type Props = { llm: LLMRow[]; stt: STTRow[]; tts: TTSRow[] };

function parseIds(raw: string | null): string[] {
  return (raw ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, MAX_COMPARE);
}

export function CompareModels({ llm, stt, tts }: Props) {
  const searchParams = useSearchParams();
  const [ids, setIds] = useState<string[]>(() =>
    parseIds(searchParams.get("m")),
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
              fontFamily: "var(--font-mono)",
              fontSize: 11,
            }}
          >
            {total} of {MAX_COMPARE} slots
          </div>
        </div>
      </div>

      <section className="cat">
        <div className="wrap">
          {selectedLLM.length > 0 && (
            <LLMCompareTable models={selectedLLM} currentIds={currentIds} />
          )}
          {selectedSTT.length > 0 && (
            <STTCompareTable models={selectedSTT} currentIds={currentIds} />
          )}
          {selectedTTS.length > 0 && (
            <TTSCompareTable models={selectedTTS} currentIds={currentIds} />
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
