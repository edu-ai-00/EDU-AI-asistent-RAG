import type { ApiClient } from "./client";
import { ApiError } from "./types";
import type { AskAnswerPart, AskExplain, AskPreviewResult, AskSource, AskTraceStep } from "./types";

const DOCUMENT_HINT = /(řád|rad\b|dokument|jídeln|jidel|pravidl|rozvrh|provoz)/i;
const NO_ANSWER_HINT = /(xyz|asdf|počasí|pocasi)/i;
const SUGGEST_HINT = /(poplat|cena|stoj)/i;
const MAX_QUESTION = 2000;

async function delay(ms = 120): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

type FirstEntry = { entry_id: string; pack_id: string; question: string } | null;

const DOC_STEPS = ["document_lexical", "rag_keywords", "rag_vector", "document_siblings"];

function ran(step: string, extra: Partial<AskTraceStep> = {}): AskTraceStep {
  return { step, status: "ran", ms: 4, ...extra };
}

function skipped(step: string, note: string): AskTraceStep {
  return { step, status: "skipped", note };
}

/** A lexical QA row shaped like search's: thresholds, weighted concepts, the n-gram chip. */
function qaLexicalDetail(hit: boolean[]): NonNullable<AskTraceStep["detail"]> {
  const concepts = [
    { c: "zacina|zacinat", df: 3, w: 0.41, hit: hit[0] },
    { c: "skolni|skola", df: 14, w: 0.12, hit: hit[1] },
    { c: "rok", df: 5, w: 0.47, hit: hit[2] },
  ];
  return { t_answer: 0.6, t_suggest: 0.3, concepts, n: 22, m_margin: 0.05, pack: "faq-provoz", candidates: 7 };
}

/** Mock of POST .../ask-preview: a curated (QA) answer by default, a document
 *  answer with sources for document-like words, a suggestion for price-like words,
 *  no_answer for nonsense; `retrieval_only` returns the sources but no answer text.
 *  Unless `debug` is false the answer carries a trace and explain like search's. */
export function createMockAsk(
  firstEntry: (school: string) => FirstEntry = () => null,
  firstDocumentUuid: (school: string) => Promise<string | null> = async () => null,
): Pick<ApiClient, "askPreview"> {
  return {
    async askPreview(school, body) {
      await delay();
      const question = body.question.trim();
      if (!question || question.length > MAX_QUESTION) {
        throw new ApiError(422, {
          code: "VALIDATION_ERROR",
          message: "The request body did not pass validation",
          retryable: false,
          fields: [{ field: "question", message: "Otázka musí mít 1 až 2000 znaků." }],
        });
      }

      const debug = body.debug !== false;
      const scope = {
        legal_entity: school,
        role: body.user_role,
        school_grade: body.user_school_grade,
        language: "cs",
        domains: null,
        identified: true,
      };
      const base = {
        delivered_by: "online",
        retrieval_only: body.retrieval_only,
        suggestions: [] as AskPreviewResult["suggestions"],
        language: "cs",
        key_words_used: [] as string[],
        strategies: [] as string[],
        answer_parts: [] as AskAnswerPart[],
        ...(debug ? { effective_scope: scope } : {}),
      };
      const explainBase: AskExplain = {
        query_norm_tokens: question.toLowerCase().split(/\s+/).filter(Boolean),
        query_lemma_tokens: question.toLowerCase().replace(/[?.!,]/g, "").split(/\s+/).filter(Boolean),
        query_language: "cs",
        query_in_corpus: true,
        query_absent_concepts: [],
        pack_ids: ["faq-provoz"],
      };

      if (NO_ANSWER_HINT.test(question)) {
        const trace: AskTraceStep[] = [
          skipped("resolve_context", "self-contained question, no prior turns, or disabled"),
          ran("route"),
          ran("qa_exact", { ms: 2 }),
          ran("qa_lexical", { ms: 9, decision: "C", match_score: 0.0, detail: qaLexicalDetail([false, false, false]) }),
          ran("qa_vector", { ms: 31 }),
          ran("document_lexical", { ms: 18, pool: 0, pool_added: 0 }),
          ran("rag_keywords", { ms: 12, pool: 0, pool_added: 0 }),
          ran("rag_vector", { ms: 40, pool: 0, pool_added: 0 }),
          skipped("document_siblings", "pool is empty"),
          ran("answer", { ms: 3, pool: 0, resolved: true, outcome: "no_answer", stopped_here: true }),
        ];
        return {
          ...base,
          answer: body.retrieval_only ? "" : "K této otázce jsem ve školních materiálech nic nenašel.",
          outcome: "no_answer",
          answer_source: "none",
          sources: [],
          suggestions: [{ kind: "qa", question: "Kdy začíná školní rok?", entry_id: null, score: 0.41 }],
          matched_entry_id: null,
          match_score: null,
          duration_ms: 180,
          ...(debug
            ? {
                trace,
                explain: { ...explainBase, query_in_corpus: false, query_absent_concepts: ["xyz"] },
              }
            : {}),
        };
      }

      if (SUGGEST_HINT.test(question)) {
        const entry = firstEntry(school);
        const trace: AskTraceStep[] = [
          skipped("resolve_context", "self-contained question, no prior turns, or disabled"),
          ran("route"),
          ran("qa_exact", { ms: 2 }),
          ran("qa_lexical", {
            ms: 11,
            resolved: true,
            outcome: "suggest",
            decision: "B",
            match_score: 0.48,
            matched_entry_id: entry?.entry_id ?? null,
            stopped_here: true,
            detail: { ...qaLexicalDetail([true, false, true]), ngram_chip: { entry_id: entry?.entry_id ?? null, score: 0.52, min_sim: 0.4, candidates: 5 } },
          }),
          ...[...["qa_vector"], ...DOC_STEPS, "answer"].map((s) => skipped(s, "cascade ended earlier")),
        ];
        return {
          ...base,
          answer: body.retrieval_only ? "" : "Našel jsem podobnou otázku. Myslíte tuto?",
          outcome: "suggest",
          answer_source: "qa_lexical",
          sources: [],
          suggestions: [
            { kind: "qa", question: entry?.question ?? "Kolik stojí školní družina?", entry_id: entry?.entry_id ?? null, score: 0.52 },
          ],
          matched_entry_id: entry?.entry_id ?? null,
          match_score: 0.48,
          duration_ms: 64,
          strategies: ["qa_lexical"],
          ...(debug ? { trace, explain: explainBase } : {}),
        };
      }

      if (DOCUMENT_HINT.test(question)) {
        const docUuid = await firstDocumentUuid(school);
        const sources: AskSource[] = [
          { rank: 1, title: "Školní řád", url: `https://www.${school.toLowerCase()}.example/skolni-rad`, ...(docUuid ? { file_uuid: docUuid } : {}) },
          { rank: 2, title: "provozni_rad_druzina.pdf", url: null, original_file_name: "provozni_rad_druzina.pdf" },
        ];
        const trace: AskTraceStep[] = [
          skipped("resolve_context", "self-contained question, no prior turns, or disabled"),
          ran("route"),
          ran("qa_exact", { ms: 2 }),
          ran("qa_lexical", { ms: 10, decision: "B-deferred", match_score: 0.34, detail: qaLexicalDetail([true, false, false]) }),
          ran("qa_vector", { ms: 29 }),
          ran("document_lexical", { ms: 21, pool: 6, pool_added: 6 }),
          ran("rag_keywords", { ms: 14, pool: 6, pool_added: 0 }),
          ran("rag_vector", { ms: 37, pool: 8, pool_added: 2 }),
          ran("document_siblings", { ms: 6, pool: 8, pool_added: 0 }),
          ran("answer", { ms: body.retrieval_only ? 3 : 2200, pool: 8, resolved: true, outcome: "answer", stopped_here: true }),
        ];
        const parts: AskAnswerPart[] = body.retrieval_only
          ? []
          : [
              { index: 0, lead_in: "Školní řád", text: "Omluvenku je nutné doručit do tří dnů od začátku nepřítomnosti.", source_rank: 1, title: "Školní řád", file_uuid: sources[0].file_uuid ?? null },
              { index: 1, lead_in: "Provoz družiny", text: "Družina je otevřena od 6:30 do 17:00.", source_rank: 2, title: "provozni_rad_druzina.pdf", file_uuid: null },
            ];
        return {
          ...base,
          answer: body.retrieval_only
            ? ""
            : "**Školní řád:** Omluvenku je nutné doručit do tří dnů od začátku nepřítomnosti.\n\n**Provoz družiny:** Družina je otevřena od 6:30 do 17:00.",
          outcome: "answer",
          answer_source: "rag",
          sources,
          suggestions: [{ kind: "related", question: "Omlouvání nepřítomnosti", entry_id: null, score: null }],
          matched_entry_id: null,
          match_score: 0.72,
          duration_ms: body.retrieval_only ? 310 : 2450,
          strategies: ["document_lexical", "rag_vector"],
          key_words_used: ["omluvenka"],
          answer_parts: parts,
          ...(debug ? { trace, explain: explainBase } : {}),
        };
      }

      const entry = firstEntry(school);
      const trace: AskTraceStep[] = [
        skipped("resolve_context", "self-contained question, no prior turns, or disabled"),
        ran("route"),
        ran("qa_exact", { ms: 2, resolved: true, outcome: "answer", match_score: 1, matched_entry_id: entry?.entry_id ?? null, stopped_here: true }),
        ...[...["qa_lexical", "qa_vector"], ...DOC_STEPS, "answer"].map((s) => skipped(s, "cascade ended earlier")),
      ];
      return {
        ...base,
        answer: body.retrieval_only ? "" : "Školní rok začíná **1. září**. Prázdniny jsou v kalendáři na webu školy.",
        outcome: "answer",
        answer_source: "qa_exact",
        sources: [{ rank: 1, title: "Kalendář školního roku", url: `https://www.${school.toLowerCase()}.example/kalendar` }],
        suggestions: [{ kind: "qa", question: "Kdy končí školní rok?", entry_id: "11111111-1111-4111-8111-111111111111", score: 0.83 }],
        matched_entry_id: entry?.entry_id ?? "22222222-2222-4222-8222-222222222222",
        match_score: 1,
        duration_ms: 35,
        strategies: ["qa_exact"],
        ...(debug ? { trace, explain: explainBase } : {}),
      };
    },
  };
}
