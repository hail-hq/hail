"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import posthog from "posthog-js";
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
    () => new Set([...llm, ...stt, ...tts].map((m) => m.model_id)),
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

  // One event per page view that arrives with a selection, not per add/remove.
  useEffect(() => {
    if (ids.length === 0) return;
    posthog.capture("compare_viewed", {
      model_count: ids.length,
      llm_count: ids.filter((id) => llm.some((m) => m.model_id === id)).length,
      stt_count: ids.filter((id) => stt.some((m) => m.model_id === id)).length,
      tts_count: ids.filter((id) => tts.some((m) => m.model_id === id)).length,
      model_ids: ids,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const add = useCallback((id: string) => {
    setIds((prev) =>
      prev.includes(id) || prev.length >= MAX_COMPARE ? prev : [...prev, id],
    );
  }, []);

  const clear = useCallback(() => {
    posthog.capture("compare_cleared");
    setIds([]);
  }, []);

  const today = new Date().toISOString().slice(0, 10);

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
      <div className="dispatch-tape">
        <div className="wrap row">
          <div className="left">
            <span className="dot">●</span> hail.so / dispatch · {today} ·
            compare
          </div>
          <div className="right">
            file: <b>compare</b> · {total} of {MAX_COMPARE} slots
          </div>
        </div>
      </div>

      <div className="wrap costs-compare-summary">
        <aside className="filed-panel">
          <b>Compare</b>
          <span>Up to {MAX_COMPARE} models, side-by-side.</span>
          <dl>
            <dt>Selected</dt>
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
            ← All costs
          </a>
          {total > 0 && (
            <button type="button" className="btn btn-outline" onClick={clear}>
              Clear
            </button>
          )}
          <div className="anchors costs-compare-anchors">
            {selectedLLM.length > 0 && <a href="#cmp-llm">LLM</a>}
            {selectedSTT.length > 0 && <a href="#cmp-stt">STT</a>}
            {selectedTTS.length > 0 && <a href="#cmp-tts">TTS</a>}
          </div>
        </div>
      </div>

      {selectedLLM.length > 0 && (
        <section className="cat" id="cmp-llm">
          <div className="wrap">
            <div className="cat-bar">
              <span className="num">01</span>
              <h2>Large language models</h2>
              <span className="count">
                {selectedLLM.length}{" "}
                {selectedLLM.length === 1 ? "model" : "models"}
              </span>
            </div>
            <LLMCompareTable models={selectedLLM} currentIds={currentIds} />
          </div>
        </section>
      )}

      {selectedSTT.length > 0 && (
        <section className="cat" id="cmp-stt">
          <div className="wrap">
            <div className="cat-bar">
              <span className="num">02</span>
              <h2>Speech to text</h2>
              <span className="count">
                {selectedSTT.length}{" "}
                {selectedSTT.length === 1 ? "model" : "models"}
              </span>
            </div>
            <STTCompareTable models={selectedSTT} currentIds={currentIds} />
          </div>
        </section>
      )}

      {selectedTTS.length > 0 && (
        <section className="cat" id="cmp-tts">
          <div className="wrap">
            <div className="cat-bar">
              <span className="num">03</span>
              <h2>Text to speech</h2>
              <span className="count">
                {selectedTTS.length}{" "}
                {selectedTTS.length === 1 ? "model" : "models"}
              </span>
            </div>
            <TTSCompareTable models={selectedTTS} currentIds={currentIds} />
          </div>
        </section>
      )}

      <section className="costs-model-picker" id="cmp-add">
        <div className="wrap">
          {total === 0 && (
            <div className="costs-compare-empty">
              <strong>No models selected</strong>
              <p>Pick at least two models below to see them side-by-side.</p>
            </div>
          )}

          <h2>{total === 0 ? "Available models" : "Add another model"}</h2>
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
    <div className="costs-model-group">
      <h3>{label}</h3>
      <div className="costs-model-options">
        {available.map((m) => (
          <button
            key={m.model_id}
            type="button"
            className="add-pill"
            disabled={currentIds.length >= MAX_COMPARE}
            onClick={() => {
              posthog.capture("model_added_to_compare", {
                model_id: m.model_id,
                provider: m.provider,
                category: label,
              });
              onAdd(m.model_id);
            }}
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
