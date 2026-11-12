import type { PendingInteraction, PendingInteractionResponse } from '@clio/core/v3';
import { useState } from 'react';
import { AttentionDraftContext, createAttentionDraftStore } from '@/components/clio/attention-drafts';
import { QuestionResponse } from '@/components/clio/pending-interaction-question-response';

const questions = [
  {
    id: 'stations',
    question: 'Which stations should the review cover?',
    allow_freeform: true,
    options: [
      { value: 'downtown', label: 'Downtown loop', description: '14 stations with the most trips' },
      { value: 'campus', label: 'Campus', description: '6 stations near the university' },
      { value: 'all', label: 'All stations', description: '41 stations, slower to compare' },
    ],
  },
  {
    id: 'window',
    question: 'Which time window?',
    allow_freeform: false,
    options: [
      { value: 'morning', label: 'Weekday mornings', description: '07:00 to 10:00' },
      { value: 'week', label: 'The whole week', description: 'Every hour, Monday to Sunday' },
    ],
  },
  {
    id: 'output',
    question: 'How should I present the findings?',
    allow_freeform: true,
    options: [
      { value: 'dashboard', label: 'Dashboard', description: 'Map and chart you can explore' },
      { value: 'notes', label: 'Short notes', description: 'A markdown file with the key numbers' },
    ],
  },
];

const interaction: PendingInteraction = {
  id: 'docs-question-dock',
  kind: 'question',
  status: 'pending',
  owner_session_id: 'docs',
  attended_session_id: 'docs',
  title: 'Plan the station review',
  source: { protocol: 'native' },
  created_at: '2026-11-12T09:00:00Z',
  actions: ['answer', 'cancel'],
  payload: { mode: 'form', metadata: { question_bundle: { questions } } },
};

type Answers = Record<string, { answer?: string; selected_options?: string[] }>;

function describe(answers: Answers) {
  return questions.map((question) => {
    const answer = answers[question.id];
    const picked = (answer?.selected_options ?? [])
      .map((value) => question.options.find((option) => option.value === value)?.label ?? value);
    return { question: question.question, answer: picked.length ? picked.join(', ') : answer?.answer || '—' };
  });
}

/** The production question dock with a three-question bundle, answered locally. */
export function QuestionDockExample() {
  const [round, setRound] = useState(0);
  const [drafts, setDrafts] = useState(() => createAttentionDraftStore('docs'));
  const [sent, setSent] = useState<ReturnType<typeof describe>>();
  const onResponse = async (_: PendingInteraction, response: PendingInteractionResponse) => {
    setSent(describe((response.metadata?.['answers'] ?? {}) as Answers));
  };
  const reset = () => {
    setSent(undefined);
    setDrafts(createAttentionDraftStore('docs'));
    setRound((value) => value + 1);
  };
  return (
    <section className="widget-question-dock" aria-label="Try the question dock">
      {sent ? (
        <div className="rounded-[10px] border border-border bg-card p-4 text-[13px] text-foreground">
          <p className="m-0 font-medium">Sent to the agent</p>
          <dl className="mt-2 mb-3 grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-x-4 gap-y-1">
            {sent.map((row) => (
              <div className="contents" key={row.question}>
                <dt className="text-muted-foreground">{row.question}</dt>
                <dd className="m-0">{row.answer}</dd>
              </div>
            ))}
          </dl>
          <button className="rounded-md border border-border px-3 py-1 text-[12.5px] hover:bg-accent" onClick={reset} type="button">
            Ask again
          </button>
        </div>
      ) : (
        <AttentionDraftContext.Provider value={drafts}>
          <QuestionResponse interaction={interaction} key={round} onResponse={onResponse} showOwner={false} />
        </AttentionDraftContext.Provider>
      )}
    </section>
  );
}
