"use client";

/**
 * The jobs the agency recruits for, the countries it sends people to, and the
 * questions it asks about a job.
 *
 * This screen is further from its consequences than any other in the product,
 * so it is built to show them. A row added here does three things somewhere
 * else: it appears in the WhatsApp bot's list within minutes, it becomes a key
 * the CV policy is resolved against, and it is written onto every candidate who
 * chooses it. None of that is visible from a form with a title field in it, so
 * each section says what it will cause — the CV rule shows its resolved answer
 * country by country, and the job list marks which rows a candidate will
 * actually be shown.
 *
 * Two sections, because the admin has two different jobs here: describing what
 * the agency recruits for, and writing the questions that get asked about it.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  Database,
  FileQuestion,
  Globe2,
  Pencil,
  Plus,
  RefreshCw,
  Smartphone,
  Trash2,
  X,
} from "lucide-react";

import {
  deleteJobQuestionAPI,
  jobCvMatrixAPI,
  listCountriesAPI,
  listJobDesignationsAPI,
  listJobQuestionsAPI,
  retireCountryAPI,
  retireJobDesignationAPI,
  saveCountryAPI,
  saveJobDesignationAPI,
  saveJobQuestionAPI,
  type CountryRow,
  type CvMatrixRow,
  type JobDesignation,
  type JobQuestion,
} from "@/lib/api";

/**
 * WhatsApp shows at most ten rows in a list and rejects an eleventh outright,
 * so nine jobs are offered and the tenth row is "Other" — a candidate whose job
 * is not shown types it and the bot maps what they typed onto a job.
 *
 * Stated here because an admin ordering thirty jobs has no other way to know
 * that only the first nine are ever seen.
 */
const BOT_VISIBLE_ROWS = 9;

type Section = "jobs" | "questions";

interface Props {
  onActivity?: (message: string, type?: "info" | "success" | "error") => void;
}

export default function DataManagementScreen({ onActivity }: Props) {
  const [section, setSection] = useState<Section>("jobs");

  const [jobs, setJobs] = useState<JobDesignation[]>([]);
  const [countries, setCountries] = useState<CountryRow[]>([]);
  const [questions, setQuestions] = useState<JobQuestion[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingJob, setEditingJob] = useState<JobDesignation | "new" | null>(null);
  const [editingCountry, setEditingCountry] = useState<CountryRow | "new" | null>(null);
  const [editingQuestion, setEditingQuestion] = useState<JobQuestion | "new" | null>(null);
  const [matrixFor, setMatrixFor] = useState<{ job: JobDesignation; rows: CvMatrixRow[] } | null>(
    null,
  );

  const say = useCallback(
    (message: string, type: "info" | "success" | "error" = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [jobRes, countryRes, questionRes] = await Promise.all([
        listJobDesignationsAPI(),
        listCountriesAPI(),
        listJobQuestionsAPI(),
      ]);
      setJobs(jobRes.items ?? []);
      setCountries(countryRes.items ?? []);
      setQuestions(questionRes.items ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Deferred into a microtask rather than called straight from the effect
    // body: `load` sets state, and doing that synchronously while the effect
    // runs makes React re-render on top of the render that scheduled it. The
    // flag drops the result of a run whose dependencies have already changed.
    let live = true;
    void (async () => {
      if (live) await load();
    })();
    return () => {
      live = false;
    };
  }, [load]);

  /** Active jobs in the order the bot will show them. */
  const orderedJobs = useMemo(
    () =>
      [...jobs]
        .filter((j) => j.active)
        .sort((a, b) => (a.bot_order ?? 100) - (b.bot_order ?? 100)),
    [jobs],
  );

  /** Which of them a candidate is actually offered — the first nine visible ones. */
  const shownInBot = useMemo(() => {
    const visible = orderedJobs.filter((j) => j.bot_visible);
    return new Set(visible.slice(0, BOT_VISIBLE_ROWS).map((j) => j.id));
  }, [orderedJobs]);

  const retiredJobs = useMemo(() => jobs.filter((j) => !j.active), [jobs]);
  const activeCountries = useMemo(() => countries.filter((c) => c.active), [countries]);

  const questionsByJob = useMemo(() => {
    const map = new Map<string, JobQuestion[]>();
    for (const q of questions) {
      const list = map.get(q.job_id) ?? [];
      list.push(q);
      map.set(q.job_id, list);
    }
    return map;
  }, [questions]);

  /* ---------------------------------------------------------------- */
  /* Writes                                                            */
  /* ---------------------------------------------------------------- */

  const saveJob = async (draft: JobDraft) => {
    try {
      await saveJobDesignationAPI({
        ...(draft.id ? { id: draft.id } : {}),
        title: draft.title.trim(),
        active: draft.active,
        bot_visible: draft.bot_visible,
        bot_order: draft.bot_order,
        cv_required_default: draft.cv_required_default,
        cv_overrides: draft.cv_overrides,
      });
      say(`Job "${draft.title}" saved`, "success");
      setEditingJob(null);
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not save the job", "error");
    }
  };

  const retireJob = async (job: JobDesignation) => {
    try {
      await retireJobDesignationAPI(job.id);
      say(`"${job.title}" retired — existing candidates keep it on their record`, "success");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not retire the job", "error");
    }
  };

  const saveCountry = async (draft: { id?: string; name: string; bot_visible: boolean; bot_order: number }) => {
    try {
      await saveCountryAPI({ ...draft, active: true });
      say(`${draft.name} saved`, "success");
      setEditingCountry(null);
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not save the country", "error");
    }
  };

  const retireCountry = async (country: CountryRow) => {
    try {
      await retireCountryAPI(country.id);
      say(`${country.name} retired`, "success");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not retire the country", "error");
    }
  };

  const saveQuestion = async (draft: QuestionDraft) => {
    try {
      await saveJobQuestionAPI({
        ...(draft.id ? { id: draft.id } : {}),
        job_id: draft.job_id,
        text: draft.text.trim(),
        kind: draft.kind,
        choices: draft.choices,
        required: draft.required,
        order: draft.order,
        active: draft.active,
      });
      say("Question saved", "success");
      setEditingQuestion(null);
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not save the question", "error");
    }
  };

  const removeQuestion = async (question: JobQuestion) => {
    try {
      await deleteJobQuestionAPI(question.id);
      say("Question removed", "success");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not remove the question", "error");
    }
  };

  const openMatrix = async (job: JobDesignation) => {
    try {
      const res = await jobCvMatrixAPI(job.id);
      setMatrixFor({ job: res.job, rows: res.matrix });
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not read the CV rules", "error");
    }
  };

  /* ---------------------------------------------------------------- */
  /* Render                                                            */
  /* ---------------------------------------------------------------- */

  if (loading) {
    return (
      <section className="db-card">
        <span className="app-boot-spinner" />
      </section>
    );
  }

  return (
    <div className="dm-screen">
      {error && (
        <section className="db-card">
          <h3 className="db-card-title">Could not load</h3>
          <p className="db-card-sub">{error}</p>
          <button type="button" className="db-btn" onClick={() => void load()}>
            Try again
          </button>
        </section>
      )}

      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Data management</h1>
          <p className="ds-head-sub">
            Job designations, destination countries, and the CV rule that applies to each pairing.
          </p>
        </div>

        <div className="ds-head-actions">
          {/* The two views this screen has, as the two states of one control.
              They used to be full-width cards, which read as destinations
              rather than as a switch between two halves of one page. */}
          <div className="ds-seg" role="group" aria-label="Section">
            <button
              type="button"
              className={`ds-seg-btn ${section === "jobs" ? "is-on" : ""}`}
              onClick={() => setSection("jobs")}
            >
              Jobs &amp; countries
            </button>
            <button
              type="button"
              className={`ds-seg-btn ${section === "questions" ? "is-on" : ""}`}
              onClick={() => setSection("questions")}
            >
              Questions
            </button>
          </div>

          <button type="button" className="ds-ghost-btn" onClick={() => void load()} title="Refresh">
            <RefreshCw size={15} /> Refresh
          </button>
        </div>
      </header>

      {section === "jobs" && (
        <>
          {/* ---- Jobs ------------------------------------------------- */}
          <div className="db-card">
            <div className="db-card-head">
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <Database size={16} />
                <h3 className="db-card-title" style={{ margin: 0 }}>Job designations</h3>
              </div>
              <button
                type="button"
                className="db-btn is-primary"
                onClick={() => setEditingJob("new")}
              >
                <Plus size={14} /> Add job
              </button>
            </div>
            <div className="db-card-body">
              <div className="db-card-sub">
                A job added here appears in the WhatsApp bot within five minutes and becomes the key
                its CV rule is looked up by. The first {BOT_VISIBLE_ROWS} shown rows are what a
                candidate is offered — WhatsApp allows ten and the tenth is “Other”, where a candidate
                types a job that is not listed.
              </div>

              <div className="staff-matrix">
                {orderedJobs.map((job) => {
                  const overrides = Object.entries(job.cv_overrides ?? {});
                  const shown = job.bot_visible && shownInBot.has(job.id);
                  return (
                    <article key={job.id} className="staff-row is-job">
                      <div className="staff-identity">
                        <div className="staff-identity-text">
                          <span className="staff-name">{job.title}</span>
                          <span className="staff-mail">
                            ID: <code>{job.id}</code>
                          </span>
                        </div>
                      </div>
                      <div className="staff-metrics">
                        <span className="staff-metric">
                          <em>Visibility</em>
                          {!job.bot_visible ? (
                            <span className="dm-pill is-off">hidden</span>
                          ) : shown ? (
                            <span className="dm-pill">
                              <Smartphone size={12} /> shown
                            </span>
                          ) : (
                            <span className="dm-pill is-off" title="Past the ninth row">
                              below cut
                            </span>
                          )}
                        </span>
                      </div>
                      <div className="staff-progress">
                        <div className="db-bar-row">
                          <button
                            type="button"
                            className="db-btn"
                            onClick={() => void openMatrix(job)}
                            title="See the answer for every destination"
                          >
                            {job.cv_required_default ? "Required" : "Not required"}
                            {overrides.length > 0 && (
                              <span className="db-tab-count" style={{ marginLeft: "6px" }}>
                                {overrides.length} exc
                              </span>
                            )}
                          </button>
                        </div>
                      </div>
                      <div className="staff-metrics">
                        <span className="staff-metric">
                          <em>Questions</em>
                          {questionsByJob.get(job.id)?.length ?? 0}
                        </span>
                      </div>
                      <div className="staff-actions">
                        <button type="button" className="db-btn" onClick={() => setEditingJob(job)}>
                          <Pencil size={13} /> Edit
                        </button>
                        <button
                          type="button"
                          className="db-btn is-danger"
                          onClick={() => void retireJob(job)}
                          title="Retire — candidates already on this job keep it"
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>

              {retiredJobs.length > 0 && (
                <div className="db-card-sub">
                  {retiredJobs.length} retired job{retiredJobs.length === 1 ? "" : "s"} —{" "}
                  {retiredJobs.map((j) => j.title).join(", ")}. They are kept because candidates are on
                  file against them.
                </div>
              )}
            </div>
          </div>

          {/* ---- Countries -------------------------------------------- */}
          <div className="db-card">
            <div className="db-card-head">
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                <Globe2 size={16} />
                <h3 className="db-card-title" style={{ margin: 0 }}>Destination countries</h3>
              </div>
              <button
                type="button"
                className="db-btn is-primary"
                onClick={() => setEditingCountry("new")}
              >
                <Plus size={14} /> Add country
              </button>
            </div>
            <div className="db-card-body">
              <div className="db-card-sub">
                One country per row, never a region. A candidate who names a single country is asked
                for their passport and their job, and the CV rule for that pairing is what decides
                whether they are asked for a CV.
              </div>

              <div className="staff-matrix">
                {activeCountries.map((country) => {
                  const key = country.name.trim().toLowerCase();
                  const exceptions = orderedJobs.filter(
                    (j) => (j.cv_overrides ?? {})[key] !== undefined,
                  );
                  return (
                    <article key={country.id} className="staff-row">
                      <div className="staff-identity">
                        <div className="staff-identity-text">
                          <span className="staff-name">{country.name}</span>
                        </div>
                      </div>
                      <div className="staff-metrics">
                        <span className="staff-metric">
                          <em>Visibility</em>
                          {country.bot_visible ? (
                            <span className="dm-pill">
                              <Smartphone size={12} /> offered
                            </span>
                          ) : (
                            <span className="dm-pill is-off">hidden</span>
                          )}
                        </span>
                      </div>
                      <div className="staff-progress">
                        <div className="db-bar-row">
                          <span className="db-bar-label" style={{ color: "var(--text)" }}>
                            <strong>Exceptions:</strong>{" "}
                            {exceptions.length === 0
                              ? "—"
                              : exceptions.map((j) => j.title).join(", ")}
                          </span>
                        </div>
                      </div>
                      <div className="staff-actions">
                        <button
                          type="button"
                          className="db-btn"
                          onClick={() => setEditingCountry(country)}
                        >
                          <Pencil size={13} /> Edit
                        </button>
                        <button
                          type="button"
                          className="db-btn is-danger"
                          onClick={() => void retireCountry(country)}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </article>
                  );
                })}
              </div>
            </div>
          </div>
        </>
      )}

      {section === "questions" && (
        <div className="db-card">
          <div className="db-card-head">
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <FileQuestion size={16} />
              <h3 className="db-card-title" style={{ margin: 0 }}>Questions asked about a job</h3>
            </div>
            <button
              type="button"
              className="db-btn is-primary"
              onClick={() => setEditingQuestion("new")}
              disabled={orderedJobs.length === 0}
            >
              <Plus size={14} /> Add question
            </button>
          </div>
          <div className="db-card-sub">
            Asked by the bot after a candidate chooses this job, and stored on their record. Write
            what a client actually asks about the role — the bot has no way to know that and an
            admin does.
          </div>

          {orderedJobs.map((job) => {
            const list = (questionsByJob.get(job.id) ?? []).sort(
              (a, b) => (a.order ?? 100) - (b.order ?? 100),
            );
            if (list.length === 0) return null;
            return (
              <div key={job.id} className="dm-question-group">
                <div className="db-card-title">{job.title}</div>
                <div className="staff-matrix">
                  {list.map((q) => (
                    <article key={q.id} className="staff-row">
                      <div className="staff-identity">
                        <div className="staff-identity-text">
                          <span className="staff-name" style={{ whiteSpace: "normal" }}>{q.text}</span>
                        </div>
                      </div>
                      <div className="staff-metrics">
                        <span className="staff-metric">
                          <em>Format</em>
                          {q.kind === "choice" ? `${q.choices.length} options` : "typed answer"}
                          {q.required ? " · required" : ""}
                          {!q.active ? " · off" : ""}
                        </span>
                      </div>
                      <div className="staff-progress" />
                      <div className="staff-actions">
                        <button
                          type="button"
                          className="db-btn"
                          onClick={() => setEditingQuestion(q)}
                        >
                          <Pencil size={13} /> Edit
                        </button>
                        <button
                          type="button"
                          className="db-btn is-danger"
                          onClick={() => void removeQuestion(q)}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              </div>
            );
          })}

          {questions.length === 0 && (
            <div className="db-empty is-compact">
              <div className="db-empty-title">No questions yet</div>
              <div className="db-empty-sub">
                Candidates are asked the standard registration questions only.
              </div>
            </div>
          )}
        </div>
      )}

      {editingJob && (
        <JobEditor
          job={editingJob === "new" ? null : editingJob}
          countries={activeCountries}
          onCancel={() => setEditingJob(null)}
          onSave={saveJob}
        />
      )}

      {editingCountry && (
        <CountryEditor
          country={editingCountry === "new" ? null : editingCountry}
          onCancel={() => setEditingCountry(null)}
          onSave={saveCountry}
        />
      )}

      {editingQuestion && (
        <QuestionEditor
          question={editingQuestion === "new" ? null : editingQuestion}
          jobs={orderedJobs}
          onCancel={() => setEditingQuestion(null)}
          onSave={saveQuestion}
        />
      )}

      {matrixFor && (
        <CvMatrixModal data={matrixFor} onClose={() => setMatrixFor(null)} />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Editors                                                             */
/* ------------------------------------------------------------------ */

interface JobDraft {
  id?: string;
  title: string;
  active: boolean;
  bot_visible: boolean;
  bot_order: number;
  cv_required_default: boolean;
  cv_overrides: Record<string, boolean>;
}

function JobEditor({
  job,
  countries,
  onCancel,
  onSave,
}: {
  job: JobDesignation | null;
  countries: CountryRow[];
  onCancel: () => void;
  onSave: (draft: JobDraft) => void | Promise<void>;
}) {
  const [title, setTitle] = useState(job?.title ?? "");
  const [botVisible, setBotVisible] = useState(job?.bot_visible ?? true);
  const [order, setOrder] = useState(job?.bot_order ?? 50);
  const [defaultRequired, setDefaultRequired] = useState(job?.cv_required_default ?? true);
  const [overrides, setOverrides] = useState<Record<string, boolean>>(job?.cv_overrides ?? {});

  /**
   * A country's rule is one of three states, and the third is the important
   * one: "follows the default" is not the same as "not required", because
   * changing the job's default has to move it.
   */
  const ruleFor = (country: CountryRow): "default" | "required" | "not_required" => {
    const value = overrides[country.name.trim().toLowerCase()];
    if (value === undefined) return "default";
    return value ? "required" : "not_required";
  };

  const setRule = (country: CountryRow, rule: "default" | "required" | "not_required") => {
    const key = country.name.trim().toLowerCase();
    setOverrides((prev) => {
      const next = { ...prev };
      if (rule === "default") delete next[key];
      else next[key] = rule === "required";
      return next;
    });
  };

  return (
    <div className="modal-container is-narrow">
      <div className="modal-header">
        <div className="modal-label">{job ? `Edit ${job.title}` : "Add a job"}</div>
        <button type="button" className="modal-close" onClick={onCancel}>
          <X size={16} />
        </button>
      </div>

      <div className="modal-body">
        <div className="field-group">
          <label className="modal-label" htmlFor="job-title">
            Job title
          </label>
          <input
            id="job-title"
            className="modal-input"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="CNC Operator"
            autoFocus
          />
          {job && (
            <div className="modal-hint">
              Id <code>{job.id}</code> — fixed. Candidates and CV rules point at it, so renaming the
              title is safe and renaming the id would not be.
            </div>
          )}
        </div>

        <div className="modal-row-2">
          <div className="field-group">
            <label className="modal-label">
              <input
                type="checkbox"
                checked={botVisible}
                onChange={(e) => setBotVisible(e.target.checked)}
              />{" "}
              Offer it in the WhatsApp bot
            </label>
            <div className="modal-hint">
              Nine jobs are shown, in this order. A candidate whose job is not listed types it
              under “Other”.
            </div>
          </div>
          <div className="field-group">
            <label className="modal-label" htmlFor="job-order">
              Position in the list
            </label>
            <input
              id="job-order"
              className="modal-input"
              type="number"
              value={order}
              onChange={(e) => setOrder(Number(e.target.value))}
            />
          </div>
        </div>

        <div className="field-group">
          <div className="modal-label">Does this job need a CV?</div>
          <select
            className="modal-select"
            value={defaultRequired ? "required" : "not_required"}
            onChange={(e) => setDefaultRequired(e.target.value === "required")}
          >
            <option value="required">CV required</option>
            <option value="not_required">CV not required</option>
          </select>
          <div className="modal-hint">
            The answer everywhere, unless a country below says otherwise.
          </div>
        </div>

        <div className="field-group">
          <div className="modal-label">Exceptions by destination</div>
          <div className="modal-hint">
            Leave a country on “follows the default” unless it genuinely differs — an exception that
            merely repeats the default stops following it the day the default changes.
          </div>
          <table className="dm-table">
            <tbody>
              {countries.map((country) => (
                <tr key={country.id}>
                  <td className="dm-cell-strong">{country.name}</td>
                  <td>
                    <select
                      className="modal-select"
                      value={ruleFor(country)}
                      onChange={(e) =>
                        setRule(country, e.target.value as "default" | "required" | "not_required")
                      }
                    >
                      <option value="default">
                        Follows the default ({defaultRequired ? "required" : "not required"})
                      </option>
                      <option value="required">CV required</option>
                      <option value="not_required">CV not required</option>
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="modal-footer">
        <button type="button" className="modal-cancel-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="db-btn is-primary"
          disabled={!title.trim()}
          onClick={() =>
            void onSave({
              ...(job ? { id: job.id } : {}),
              title,
              active: true,
              bot_visible: botVisible,
              bot_order: order,
              cv_required_default: defaultRequired,
              cv_overrides: overrides,
            })
          }
        >
          <Check size={14} /> Save
        </button>
      </div>
    </div>
  );
}

function CountryEditor({
  country,
  onCancel,
  onSave,
}: {
  country: CountryRow | null;
  onCancel: () => void;
  onSave: (draft: { id?: string; name: string; bot_visible: boolean; bot_order: number }) => void;
}) {
  const [name, setName] = useState(country?.name ?? "");
  const [botVisible, setBotVisible] = useState(country?.bot_visible ?? true);
  const [order, setOrder] = useState(country?.bot_order ?? 50);

  return (
    <div className="modal-container is-narrow">
      <div className="modal-header">
        <div className="modal-label">{country ? `Edit ${country.name}` : "Add a country"}</div>
        <button type="button" className="modal-close" onClick={onCancel}>
          <X size={16} />
        </button>
      </div>

      <div className="modal-body">
        <div className="field-group">
          <label className="modal-label" htmlFor="country-name">
            Country
          </label>
          <input
            id="country-name"
            className="modal-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Kuwait"
            autoFocus
          />
          <div className="modal-hint">
            One country, not a region. “The Gulf” is six countries with six sets of rules, and a CV
            rule cannot be written for a record that does not say which one.
          </div>
        </div>

        <div className="modal-row-2">
          <div className="field-group">
            <label className="modal-label">
              <input
                type="checkbox"
                checked={botVisible}
                onChange={(e) => setBotVisible(e.target.checked)}
              />{" "}
              Offer it in the WhatsApp bot
            </label>
          </div>
          <div className="field-group">
            <label className="modal-label" htmlFor="country-order">
              Position in the list
            </label>
            <input
              id="country-order"
              className="modal-input"
              type="number"
              value={order}
              onChange={(e) => setOrder(Number(e.target.value))}
            />
          </div>
        </div>
      </div>

      <div className="modal-footer">
        <button type="button" className="modal-cancel-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="db-btn is-primary"
          disabled={!name.trim()}
          onClick={() =>
            onSave({
              ...(country ? { id: country.id } : {}),
              name,
              bot_visible: botVisible,
              bot_order: order,
            })
          }
        >
          <Check size={14} /> Save
        </button>
      </div>
    </div>
  );
}

interface QuestionDraft {
  id?: string;
  job_id: string;
  text: string;
  kind: "text" | "choice";
  choices: string[];
  required: boolean;
  order: number;
  active: boolean;
}

function QuestionEditor({
  question,
  jobs,
  onCancel,
  onSave,
}: {
  question: JobQuestion | null;
  jobs: JobDesignation[];
  onCancel: () => void;
  onSave: (draft: QuestionDraft) => void;
}) {
  const [jobId, setJobId] = useState(question?.job_id ?? jobs[0]?.id ?? "");
  const [text, setText] = useState(question?.text ?? "");
  const [kind, setKind] = useState<"text" | "choice">(question?.kind ?? "text");
  const [choices, setChoices] = useState((question?.choices ?? []).join("\n"));
  const [required, setRequired] = useState(question?.required ?? false);
  const [order, setOrder] = useState(question?.order ?? 50);

  const choiceList = choices
    .split("\n")
    .map((c) => c.trim())
    .filter(Boolean);

  return (
    <div className="modal-container is-narrow">
      <div className="modal-header">
        <div className="modal-label">{question ? "Edit question" : "Add a question"}</div>
        <button type="button" className="modal-close" onClick={onCancel}>
          <X size={16} />
        </button>
      </div>

      <div className="modal-body">
        <div className="field-group">
          <label className="modal-label" htmlFor="q-job">
            Asked of candidates who choose
          </label>
          <select
            id="q-job"
            className="modal-select"
            value={jobId}
            onChange={(e) => setJobId(e.target.value)}
          >
            {jobs.map((job) => (
              <option key={job.id} value={job.id}>
                {job.title}
              </option>
            ))}
          </select>
        </div>

        <div className="field-group">
          <label className="modal-label" htmlFor="q-text">
            Question
          </label>
          <input
            id="q-text"
            className="modal-input"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Which controllers have you run — Fanuc, Siemens, Haas?"
            autoFocus
          />
          <div className="modal-hint">
            Written as you would say it out loud. It is sent to the candidate exactly as typed.
          </div>
        </div>

        <div className="modal-row-2">
          <div className="field-group">
            <label className="modal-label" htmlFor="q-kind">
              Answer
            </label>
            <select
              id="q-kind"
              className="modal-select"
              value={kind}
              onChange={(e) => setKind(e.target.value as "text" | "choice")}
            >
              <option value="text">They type it</option>
              <option value="choice">They tap one of your options</option>
            </select>
          </div>
          <div className="field-group">
            <label className="modal-label" htmlFor="q-order">
              Order
            </label>
            <input
              id="q-order"
              className="modal-input"
              type="number"
              value={order}
              onChange={(e) => setOrder(Number(e.target.value))}
            />
          </div>
        </div>

        {kind === "choice" && (
          <div className="field-group">
            <label className="modal-label" htmlFor="q-choices">
              Options, one per line
            </label>
            <textarea
              id="q-choices"
              className="modal-input"
              rows={5}
              value={choices}
              onChange={(e) => setChoices(e.target.value)}
              placeholder={"Fanuc\nSiemens\nHaas"}
            />
            <div className="modal-hint">
              Up to nine. WhatsApp shows ten rows and one is kept for “Talk to staff”.
              {choiceList.length > 9 && (
                <strong> {choiceList.length} entered — only the first nine will be shown.</strong>
              )}
            </div>
          </div>
        )}

        <div className="field-group">
          <label className="modal-label">
            <input
              type="checkbox"
              checked={required}
              onChange={(e) => setRequired(e.target.checked)}
            />{" "}
            The candidate must answer it
          </label>
        </div>
      </div>

      <div className="modal-footer">
        <button type="button" className="modal-cancel-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="db-btn is-primary"
          disabled={!text.trim() || !jobId || (kind === "choice" && choiceList.length === 0)}
          onClick={() =>
            onSave({
              ...(question ? { id: question.id } : {}),
              job_id: jobId,
              text,
              kind,
              choices: choiceList,
              required,
              order,
              active: true,
            })
          }
        >
          <Check size={14} /> Save
        </button>
      </div>
    </div>
  );
}

/**
 * What a job's rules actually resolve to, destination by destination.
 *
 * The form takes a default and a handful of exceptions; this is the answer a
 * candidate will get. Computed by the CRM through the same function the bot's
 * request goes through, so this screen cannot drift from the decision.
 */
function CvMatrixModal({
  data,
  onClose,
}: {
  data: { job: JobDesignation; rows: CvMatrixRow[] };
  onClose: () => void;
}) {
  return (
    <div className="modal-container is-narrow">
      <div className="modal-header">
        <div className="modal-label">{data.job.title} — CV rule by destination</div>
        <button type="button" className="modal-close" onClick={onClose}>
          <X size={16} />
        </button>
      </div>
      <div className="modal-body">
        <table className="dm-table">
          <thead>
            <tr>
              <th>Destination</th>
              <th>CV</th>
              <th>Because</th>
            </tr>
          </thead>
          <tbody>
            {data.rows.map((row) => (
              <tr key={row.country}>
                <td className="dm-cell-strong">{row.country}</td>
                <td>{row.cv_required ? "Required" : "Not required"}</td>
                <td className="dm-cell-muted">
                  {row.is_override ? "exception set here" : "the job’s default"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="modal-hint">
          This is what a candidate registering right now would be asked. Candidates already
          registered keep the answer that applied on the day — changing a rule never rewrites a
          record.
        </div>
      </div>
      <div className="modal-footer">
        <button type="button" className="db-btn is-primary" onClick={onClose}>
          <ChevronDown size={14} /> Close
        </button>
      </div>
    </div>
  );
}
