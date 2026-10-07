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
 * Five sections cover recruitment data, screening questions, the mobile
 * numbers that must never enter the bot's candidate conversation, the office
 * branches employees are grouped into, and the government holidays on which
 * no attendance is owed.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Ban,
  Building2,
  CalendarDays,
  Check,
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
  addBotSuppressionNumberAPI,
  createBranch,
  createHoliday,
  deleteBranch,
  deleteHoliday,
  fetchBranches,
  fetchHolidays,
  deleteBotSuppressionNumberAPI,
  deleteJobQuestionAPI,
  listCountriesAPI,
  listBotSuppressionNumbersAPI,
  listJobDesignationsAPI,
  listJobQuestionsAPI,
  retireCountryAPI,
  retireJobDesignationAPI,
  saveCountryAPI,
  saveJobDesignationAPI,
  saveJobQuestionAPI,
  type BotSuppressionNumber,
  type BuiltinHoliday,
  type CountryRow,
  type GovernmentHoliday,
  type JobDesignation,
  type JobQuestion,
} from "@/lib/api";
import { BRANCHES, BRANCHES_CHANGED, sameBranch } from "@/components/ui/BranchSwitch";
import DatePicker from "@/components/ui/DatePicker";
import Select from "@/components/ui/Select";
import { useModalFocus } from "@/components/ui/useModalFocus";

/**
 * WhatsApp shows at most ten rows in a list and rejects an eleventh outright,
 * so nine jobs are offered and the tenth row is "Other" — a candidate whose job
 * is not shown types it and the bot maps what they typed onto a job.
 *
 * Stated here because an admin ordering thirty jobs has no other way to know
 * that only the first nine are ever seen.
 */
const BOT_VISIBLE_ROWS = 9;

type Section = "jobs" | "questions" | "suppression" | "branches" | "holidays";

interface Props {
  onActivity?: (message: string, type?: "info" | "success" | "error") => void;
}

export default function DataManagementScreen({ onActivity }: Props) {
  const [section, setSection] = useState<Section>("jobs");

  const [jobs, setJobs] = useState<JobDesignation[]>([]);
  const [countries, setCountries] = useState<CountryRow[]>([]);
  const [questions, setQuestions] = useState<JobQuestion[]>([]);
  const [suppressedNumbers, setSuppressedNumbers] = useState<BotSuppressionNumber[]>([]);
  const [suppressionPhone, setSuppressionPhone] = useState("");
  const [suppressionLabel, setSuppressionLabel] = useState("");
  const [suppressionSaving, setSuppressionSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [editingJob, setEditingJob] = useState<JobDesignation | "new" | null>(null);
  const [editingCountry, setEditingCountry] = useState<CountryRow | "new" | null>(null);
  const [editingQuestion, setEditingQuestion] = useState<JobQuestion | "new" | null>(null);

  const say = useCallback(
    (message: string, type: "info" | "success" | "error" = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [jobRes, countryRes, questionRes, suppressionRes] = await Promise.all([
        listJobDesignationsAPI(),
        listCountriesAPI(),
        listJobQuestionsAPI(),
        listBotSuppressionNumbersAPI(),
      ]);
      setJobs(jobRes.items ?? []);
      setCountries(countryRes.items ?? []);
      setQuestions(questionRes.items ?? []);
      setSuppressedNumbers(suppressionRes.items ?? []);
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

  const addSuppressedNumber = async () => {
    const phone = suppressionPhone.trim();
    if (!phone || suppressionSaving) return;
    setSuppressionSaving(true);
    try {
      await addBotSuppressionNumberAPI(phone, suppressionLabel.trim());
      setSuppressionPhone("");
      setSuppressionLabel("");
      say(`${phone} will no longer trigger the recruitment bot`, "success");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not suppress the number", "error");
    } finally {
      setSuppressionSaving(false);
    }
  };

  const removeSuppressedNumber = async (item: BotSuppressionNumber) => {
    if (!window.confirm(`Allow bot replies from ${item.phone} again?`)) return;
    try {
      await deleteBotSuppressionNumberAPI(item.id);
      say(`${item.phone} can trigger the bot again`, "success");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not remove the number", "error");
    }
  };

  /* ---------------------------------------------------------------- */
  /* Render                                                            */
  /* ---------------------------------------------------------------- */

  if (loading) {
    return (
      <section className="db-card db-feedback is-loading" role="status" aria-live="polite">
        <span className="app-boot-spinner" aria-hidden="true" />
        <div><strong>Loading recruitment data</strong><span>Preparing jobs, destinations, and rules…</span></div>
      </section>
    );
  }

  return (
    <div className="dm-screen">
      {error && (
        <section className="db-card db-feedback is-error" role="alert">
          <span className="db-feedback-icon"><AlertTriangle size={20} /></span>
          <div><strong>Could not load recruitment data</strong><span>{error}</span></div>
          <button type="button" className="db-btn" onClick={() => void load()}>
            <RefreshCw size={14} /> Try again
          </button>
        </section>
      )}

      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Data management</h1>
          <p className="ds-head-sub">
            Recruitment choices, screening questions, CV rules, and bot suppression numbers.
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
            <button
              type="button"
              className={`ds-seg-btn ${section === "suppression" ? "is-on" : ""}`}
              onClick={() => setSection("suppression")}
            >
              Bot suppression
            </button>
            <button
              type="button"
              className={`ds-seg-btn ${section === "branches" ? "is-on" : ""}`}
              onClick={() => setSection("branches")}
            >
              Branches
            </button>
            <button
              type="button"
              className={`ds-seg-btn ${section === "holidays" ? "is-on" : ""}`}
              onClick={() => setSection("holidays")}
            >
              Holidays
            </button>
          </div>

          <button type="button" className="ds-ghost-btn" onClick={() => void load()} title="Refresh">
            <RefreshCw size={15} /> Refresh
          </button>
        </div>
      </header>

      <div className="ds-stats dm-stats" aria-label="Configuration summary">
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Active jobs</span>
            <span className="ds-stat-icon" aria-hidden="true"><Database size={16} /></span>
          </span>
          <span className="ds-stat-value">{orderedJobs.length}</span>
          <span className="ds-stat-foot">Recruitment designations in use</span>
        </section>
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Bot menu</span>
            <span className="ds-stat-icon" aria-hidden="true"><Smartphone size={16} /></span>
          </span>
          <span className="ds-stat-value">{shownInBot.size}/{BOT_VISIBLE_ROWS}</span>
          <span className="ds-stat-foot">Visible job slots currently used</span>
        </section>
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Destinations</span>
            <span className="ds-stat-icon" aria-hidden="true"><Globe2 size={16} /></span>
          </span>
          <span className="ds-stat-value">{activeCountries.length}</span>
          <span className="ds-stat-foot">Active destination countries</span>
        </section>
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Screening questions</span>
            <span className="ds-stat-icon" aria-hidden="true"><FileQuestion size={16} /></span>
          </span>
          <span className="ds-stat-value">{questions.filter((question) => question.active).length}</span>
          <span className="ds-stat-foot">Active job-specific questions</span>
        </section>
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Suppressed numbers</span>
            <span className="ds-stat-icon" aria-hidden="true"><Ban size={16} /></span>
          </span>
          <span className="ds-stat-value">{suppressedNumbers.length}</span>
          <span className="ds-stat-foot">Senders the bot will leave unanswered</span>
        </section>
      </div>

      {section === "jobs" && (
        <>
          {/* ---- Jobs ------------------------------------------------- */}
          <section className="db-card dm-panel">
            <div className="db-card-head dm-panel-head">
              <div className="dm-panel-title">
                <Database size={16} />
                <div>
                  <h3 className="db-card-title">Job designations</h3>
                  <p>Control menu visibility, CV rules, and job-specific screening.</p>
                </div>
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

              {orderedJobs.length > 0 && (
                <div className="dm-table-frame">
                  <table className="dm-table is-register dm-job-table">
                    <colgroup>
                      <col className="dm-col-job" />
                      <col className="dm-col-menu" />
                      <col className="dm-col-policy" />
                      <col className="dm-col-count" />
                      <col className="dm-col-actions" />
                    </colgroup>
                    <thead>
                      <tr>
                        <th>Job designation</th>
                        <th>Bot menu</th>
                        <th>CV policy</th>
                        <th className="is-center">Questions</th>
                        <th className="is-actions">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {orderedJobs.map((job) => {
                        const overrides = Object.entries(job.cv_overrides ?? {});
                        const shown = job.bot_visible && shownInBot.has(job.id);
                        return (
                          <tr key={job.id}>
                            <td>
                              <span className="dm-primary-cell">
                                <strong>{job.title}</strong>
                                <small>ID: {job.id}</small>
                              </span>
                            </td>
                            <td>
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
                            </td>
                            <td>
                              <span className={`dm-policy ${job.cv_required_default ? "is-required" : ""}`}>
                                {job.cv_required_default ? <Check size={13} /> : <X size={13} />}
                                {job.cv_required_default ? "Required" : "Not required"}
                                {overrides.length > 0 && (
                                  <em>{overrides.length} exception{overrides.length === 1 ? "" : "s"}</em>
                                )}
                              </span>
                            </td>
                            <td className="is-center is-num">
                              {questionsByJob.get(job.id)?.length ?? 0}
                            </td>
                            <td className="is-actions">
                              <div className="dm-cell-actions">
                                <button type="button" className="db-btn" onClick={() => setEditingJob(job)}>
                                  <Pencil size={13} /> Edit
                                </button>
                                <button
                                  type="button"
                                  className="db-btn is-danger"
                                  onClick={() => void retireJob(job)}
                                  title="Retire — candidates already on this job keep it"
                                  aria-label={`Retire ${job.title}`}
                                >
                                  <Trash2 size={13} />
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {orderedJobs.length === 0 && (
                <div className="ds-empty-state dm-empty">
                  <Database size={28} />
                  <h3>No active jobs</h3>
                  <p>Add a designation to start routing candidate registrations.</p>
                  <button type="button" className="ds-primary-btn" onClick={() => setEditingJob("new")}>
                    <Plus size={14} /> Add job
                  </button>
                </div>
              )}

              {retiredJobs.length > 0 && (
                <div className="dm-retired-note">
                  <AlertTriangle size={14} />
                  <span>
                  {retiredJobs.length} retired job{retiredJobs.length === 1 ? "" : "s"} —{" "}
                  {retiredJobs.map((j) => j.title).join(", ")}. They are kept because candidates are on
                  file against them.
                  </span>
                </div>
              )}
            </div>
          </section>

          {/* ---- Countries -------------------------------------------- */}
          <section className="db-card dm-panel">
            <div className="db-card-head dm-panel-head">
              <div className="dm-panel-title">
                <Globe2 size={16} />
                <div>
                  <h3 className="db-card-title">Destination countries</h3>
                  <p>Manage registration destinations and country-specific CV exceptions.</p>
                </div>
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

              {activeCountries.length > 0 && (
                <div className="dm-table-frame">
                  <table className="dm-table is-register dm-country-table">
                    <colgroup>
                      <col className="dm-col-country" />
                      <col className="dm-col-menu" />
                      <col className="dm-col-exceptions" />
                      <col className="dm-col-actions" />
                    </colgroup>
                    <thead>
                      <tr>
                        <th>Destination</th>
                        <th>Bot menu</th>
                        <th>CV exceptions</th>
                        <th className="is-actions">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeCountries.map((country) => {
                        const key = country.name.trim().toLowerCase();
                        const exceptions = orderedJobs.filter(
                          (job) => (job.cv_overrides ?? {})[key] !== undefined,
                        );
                        return (
                          <tr key={country.id}>
                            <td>
                              <span className="dm-primary-cell">
                                <strong>{country.name}</strong>
                              </span>
                            </td>
                            <td>
                              {country.bot_visible ? (
                                <span className="dm-pill">
                                  <Smartphone size={12} /> offered
                                </span>
                              ) : (
                                <span className="dm-pill is-off">hidden</span>
                              )}
                            </td>
                            <td className="is-wrap">
                              {exceptions.length === 0
                                ? "—"
                                : exceptions.map((job) => job.title).join(", ")}
                            </td>
                            <td className="is-actions">
                              <div className="dm-cell-actions">
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
                                  aria-label={`Retire ${country.name}`}
                                >
                                  <Trash2 size={13} />
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}

              {activeCountries.length === 0 && (
                <div className="ds-empty-state dm-empty">
                  <Globe2 size={28} />
                  <h3>No active destinations</h3>
                  <p>Add a country to make it available during candidate registration.</p>
                  <button type="button" className="ds-primary-btn" onClick={() => setEditingCountry("new")}>
                    <Plus size={14} /> Add country
                  </button>
                </div>
              )}
            </div>
          </section>
        </>
      )}

      {section === "questions" && (
        <section className="db-card dm-panel">
          <div className="db-card-head dm-panel-head">
            <div className="dm-panel-title">
              <FileQuestion size={16} />
              <div>
                <h3 className="db-card-title">Job screening questions</h3>
                <p>Collect role-specific answers after a candidate chooses their job.</p>
              </div>
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
                <div className="dm-question-head">
                  <span>{job.title}</span>
                  <em>{list.length} question{list.length === 1 ? "" : "s"}</em>
                </div>
                <div className="dm-table-frame">
                  <table className="dm-table is-register dm-question-table">
                    <colgroup>
                      <col className="dm-col-question" />
                      <col className="dm-col-format" />
                      <col className="dm-col-actions" />
                    </colgroup>
                    <thead>
                      <tr>
                        <th>Question</th>
                        <th>Answer format</th>
                        <th className="is-actions">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {list.map((question) => (
                        <tr key={question.id}>
                          <td className="is-wrap">
                            <span className="dm-primary-cell">
                              <strong>{question.text}</strong>
                            </span>
                          </td>
                          <td>
                            {question.kind === "choice"
                              ? `${question.choices.length} options`
                              : "Typed answer"}
                            {question.required ? " · Required" : ""}
                            {!question.active ? " · Off" : ""}
                          </td>
                          <td className="is-actions">
                            <div className="dm-cell-actions">
                              <button
                                type="button"
                                className="db-btn"
                                onClick={() => setEditingQuestion(question)}
                              >
                                <Pencil size={13} /> Edit
                              </button>
                              <button
                                type="button"
                                className="db-btn is-danger"
                                onClick={() => void removeQuestion(question)}
                                aria-label={`Delete question: ${question.text}`}
                              >
                                <Trash2 size={13} />
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            );
          })}

          {questions.length === 0 && (
            <div className="ds-empty-state dm-empty">
              <FileQuestion size={28} />
              <h3>No job-specific questions</h3>
              <p>Candidates currently receive only the standard registration questions.</p>
              {orderedJobs.length > 0 && (
                <button type="button" className="ds-primary-btn" onClick={() => setEditingQuestion("new")}>
                  <Plus size={14} /> Add question
                </button>
              )}
            </div>
          )}
        </section>
      )}

      {section === "branches" && <BranchesPanel onActivity={onActivity} />}

      {section === "holidays" && <HolidaysPanel onActivity={onActivity} />}

      {section === "suppression" && (
        <section className="db-card dm-panel">
          <div className="db-card-head dm-panel-head">
            <div className="dm-panel-title">
              <Ban size={16} />
              <div>
                <h3 className="db-card-title">Bot suppression numbers</h3>
                <p>Messages from these mobile numbers are ignored before a bot conversation starts.</p>
              </div>
            </div>
          </div>
          <div className="db-card-body">
            <div className="db-card-sub">
              Add personal, vendor, former staff, or test numbers that must never receive the
              recruitment flow. Formatting and country-code differences are normalized automatically.
            </div>

            <div className="dm-suppression-add">
              <label>
                <span>Mobile number</span>
                <input
                  className="modal-input"
                  type="tel"
                  value={suppressionPhone}
                  onChange={(event) => setSuppressionPhone(event.target.value)}
                  placeholder="+91 98765 43210"
                  disabled={suppressionSaving}
                />
              </label>
              <label>
                <span>Label (optional)</span>
                <input
                  className="modal-input"
                  value={suppressionLabel}
                  onChange={(event) => setSuppressionLabel(event.target.value)}
                  placeholder="Example: Former staff"
                  disabled={suppressionSaving}
                />
              </label>
              <button
                type="button"
                className="db-btn is-primary"
                onClick={() => void addSuppressedNumber()}
                disabled={!suppressionPhone.trim() || suppressionSaving}
              >
                <Plus size={14} /> {suppressionSaving ? "Adding…" : "Add number"}
              </button>
            </div>

            {suppressedNumbers.length > 0 ? (
              <div className="dm-table-frame">
                <table className="dm-table is-register">
                  <thead>
                    <tr>
                      <th>Mobile number</th>
                      <th>Label</th>
                      <th>Added by</th>
                      <th className="is-actions">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {suppressedNumbers.map((item) => (
                      <tr key={item.id}>
                        <td><span className="dm-primary-cell"><strong>{item.phone}</strong></span></td>
                        <td>{item.label || "—"}</td>
                        <td>{item.created_by || "—"}</td>
                        <td className="is-actions">
                          <button
                            type="button"
                            className="db-btn is-danger"
                            onClick={() => void removeSuppressedNumber(item)}
                            aria-label={`Remove ${item.phone} from bot suppression`}
                          >
                            <Trash2 size={13} /> Remove
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="ds-empty-state dm-empty">
                <Ban size={28} />
                <h3>No manually suppressed numbers</h3>
                <p>Internal staff and Sourcing Hub contacts are still suppressed automatically.</p>
              </div>
            )}
          </div>
        </section>
      )}

      {editingJob && (
        <div className="cm-overlay active" onClick={() => setEditingJob(null)}>
          <JobEditor
            job={editingJob === "new" ? null : editingJob}
            countries={activeCountries}
            onCancel={() => setEditingJob(null)}
            onSave={saveJob}
          />
        </div>
      )}

      {editingCountry && (
        <div className="cm-overlay active" onClick={() => setEditingCountry(null)}>
          <CountryEditor
            country={editingCountry === "new" ? null : editingCountry}
            onCancel={() => setEditingCountry(null)}
            onSave={saveCountry}
          />
        </div>
      )}

      {editingQuestion && (
        <div className="cm-overlay active" onClick={() => setEditingQuestion(null)}>
          <QuestionEditor
            question={editingQuestion === "new" ? null : editingQuestion}
            jobs={orderedJobs}
            onCancel={() => setEditingQuestion(null)}
            onSave={saveQuestion}
          />
        </div>
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
  const dialogRef = useModalFocus<HTMLDivElement>(true, onCancel);

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
    <div ref={dialogRef} className="cm-dialog dm-dialog" role="dialog" aria-modal="true" aria-labelledby="job-editor-title" tabIndex={-1} onClick={(event) => event.stopPropagation()}>
      <div className="modal-header">
        <h2 className="modal-title" id="job-editor-title">{job ? `Edit ${job.title}` : "Add a job"}</h2>
        <button type="button" className="modal-close" onClick={onCancel} aria-label="Close job editor">
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
            data-dialog-initial-focus
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
          <Select
            value={defaultRequired ? "required" : "not_required"}
            options={[
              { value: "required", label: "CV required" },
              { value: "not_required", label: "CV not required" },
            ]}
            onChange={(value) => setDefaultRequired(value === "required")}
            ariaLabel="Default CV requirement"
          />
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
                    <Select
                      value={ruleFor(country)}
                      options={[
                        {
                          value: "default",
                          label: `Follows default (${defaultRequired ? "required" : "not required"})`,
                        },
                        { value: "required", label: "CV required" },
                        { value: "not_required", label: "CV not required" },
                      ]}
                      onChange={(value) =>
                        setRule(country, value as "default" | "required" | "not_required")
                      }
                      ariaLabel={`CV rule for ${country.name}`}
                    />
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
  const dialogRef = useModalFocus<HTMLDivElement>(true, onCancel);

  return (
    <div ref={dialogRef} className="cm-dialog dm-dialog is-compact" role="dialog" aria-modal="true" aria-labelledby="country-editor-title" tabIndex={-1} onClick={(event) => event.stopPropagation()}>
      <div className="modal-header">
        <h2 className="modal-title" id="country-editor-title">{country ? `Edit ${country.name}` : "Add a country"}</h2>
        <button type="button" className="modal-close" onClick={onCancel} aria-label="Close country editor">
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
            data-dialog-initial-focus
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
  const dialogRef = useModalFocus<HTMLDivElement>(true, onCancel);

  const choiceList = choices
    .split("\n")
    .map((c) => c.trim())
    .filter(Boolean);

  return (
    <div ref={dialogRef} className="cm-dialog dm-dialog" role="dialog" aria-modal="true" aria-labelledby="question-editor-title" tabIndex={-1} onClick={(event) => event.stopPropagation()}>
      <div className="modal-header">
        <h2 className="modal-title" id="question-editor-title">{question ? "Edit question" : "Add a question"}</h2>
        <button type="button" className="modal-close" onClick={onCancel} aria-label="Close question editor">
          <X size={16} />
        </button>
      </div>

      <div className="modal-body">
        <div className="field-group">
          <label className="modal-label" htmlFor="q-job">
            Asked of candidates who choose
          </label>
          <Select
            id="q-job"
            value={jobId}
            options={jobs.map((job) => ({ value: job.id, label: job.title }))}
            onChange={setJobId}
            ariaLabel="Job for this question"
          />
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
            data-dialog-initial-focus
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
            <Select
              id="q-kind"
              value={kind}
              options={[
                { value: "text", label: "They type it" },
                { value: "choice", label: "They tap one of your options" },
              ]}
              onChange={(value) => setKind(value as "text" | "choice")}
              ariaLabel="Answer type"
            />
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
 * The offices employees belong to. Every branch dropdown in User Management,
 * Attendance and Payroll lists these; the two original branches are fixed.
 */
function BranchesPanel({ onActivity }: Props) {
  const [branches, setBranches] = useState<string[]>([...BRANCHES]);
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetchBranches()
      .then((result) => { if (live) setBranches(result.items); })
      .catch((err) => { if (live) setError(err instanceof Error ? err.message : String(err)); });
    return () => {
      live = false;
    };
  }, []);

  const changed = (items: string[]) => {
    setBranches(items);
    window.dispatchEvent(new Event(BRANCHES_CHANGED));
  };

  const add = async () => {
    if (!name.trim() || saving) return;
    setSaving(true);
    setError(null);
    try {
      const result = await createBranch(name.trim());
      changed(result.items);
      onActivity?.(`Branch ${result.name} added`, "success");
      setName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the branch");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (branch: string) => {
    if (!window.confirm(`Remove the ${branch} branch?`)) return;
    setError(null);
    try {
      const result = await deleteBranch(branch);
      changed(result.items);
      onActivity?.(`Branch ${branch} removed`, "success");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the branch");
    }
  };

  return (
    <section className="db-card dm-panel">
      <div className="db-card-head dm-panel-head">
        <div className="dm-panel-title">
          <Building2 size={16} />
          <div>
            <h3 className="db-card-title">Branches</h3>
            <p>The offices staff are assigned to. Every branch dropdown in the CRM lists these.</p>
          </div>
        </div>
      </div>
      <div className="db-card-body">
        <div className="dm-suppression-add">
          <label>
            <span>Branch name</span>
            <input
              className="modal-input"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => { if (event.key === "Enter") void add(); }}
              placeholder="Example: Anna Nagar"
              maxLength={100}
              disabled={saving}
            />
          </label>
          <button
            type="button"
            className="db-btn is-primary"
            onClick={() => void add()}
            disabled={!name.trim() || saving}
          >
            <Plus size={14} /> {saving ? "Adding…" : "Add branch"}
          </button>
        </div>
        {error && <p className="sh-form-error" role="alert">{error}</p>}
        <div className="dm-table-frame">
          <table className="dm-table is-register">
            <thead>
              <tr>
                <th>Branch</th>
                <th>Type</th>
                <th className="is-actions">Actions</th>
              </tr>
            </thead>
            <tbody>
              {branches.map((branch) => {
                const original = BRANCHES.some((fixed) => sameBranch(fixed, branch));
                return (
                  <tr key={branch}>
                    <td><span className="dm-primary-cell"><strong>{branch}</strong></span></td>
                    <td>{original ? "Original branch" : "Added"}</td>
                    <td className="is-actions">
                      {!original && (
                        <button
                          type="button"
                          className="db-btn is-danger"
                          onClick={() => void remove(branch)}
                          aria-label={`Remove the ${branch} branch`}
                        >
                          <Trash2 size={13} /> Remove
                        </button>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

/** "2026-10-20" -> "Tue, 20 Oct 2026", read as a calendar date (no timezone shift). */
function formatHolidayDate(iso: string): string {
  const [year, month, day] = iso.split("-").map(Number);
  if (!year || !month || !day) return iso;
  return new Date(year, month - 1, day).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

/**
 * Public holidays observed in India (national, Tamil Nadu and the major
 * religious festivals). `fixed` is the month/day for holidays that never move,
 * so picking one can fill the date in; the rest follow the lunar calendar and
 * the date has to be chosen from that year's government notification.
 */
const INDIAN_HOLIDAYS: { name: string; hint: string; fixed?: [number, number] }[] = [
  { name: "New Year's Day", hint: "1 January", fixed: [1, 1] },
  { name: "Pongal", hint: "Usually 14–15 January" },
  { name: "Thiruvalluvar Day", hint: "Usually 15–16 January" },
  { name: "Uzhavar Thirunal", hint: "Usually 16–17 January" },
  { name: "Republic Day", hint: "26 January", fixed: [1, 26] },
  { name: "Thai Poosam", hint: "January / February" },
  { name: "Maha Shivaratri", hint: "February / March" },
  { name: "Holi", hint: "March" },
  { name: "Telugu New Year (Ugadi)", hint: "March / April" },
  { name: "Ramzan (Idu'l Fitr)", hint: "Varies — lunar calendar" },
  { name: "Mahavir Jayanthi", hint: "March / April" },
  { name: "Good Friday", hint: "March / April" },
  { name: "Tamil New Year", hint: "14 April", fixed: [4, 14] },
  { name: "Dr. B.R. Ambedkar Jayanthi", hint: "14 April", fixed: [4, 14] },
  { name: "May Day", hint: "1 May", fixed: [5, 1] },
  { name: "Buddha Purnima", hint: "April / May" },
  { name: "Bakrid (Idul Azha)", hint: "Varies — lunar calendar" },
  { name: "Muharram", hint: "Varies — lunar calendar" },
  { name: "Independence Day", hint: "15 August", fixed: [8, 15] },
  { name: "Krishna Jayanthi", hint: "August / September" },
  { name: "Vinayagar Chathurthi", hint: "August / September" },
  { name: "Milad-un-Nabi", hint: "Varies — lunar calendar" },
  { name: "Gandhi Jayanthi", hint: "2 October", fixed: [10, 2] },
  { name: "Ayudha Pooja", hint: "September / October" },
  { name: "Vijaya Dasami", hint: "September / October" },
  { name: "Deepavali", hint: "October / November" },
  { name: "Guru Nanak Jayanthi", hint: "November" },
  { name: "Christmas", hint: "25 December", fixed: [12, 25] },
  { name: "Election Day", hint: "As notified" },
];

const OTHER_HOLIDAY = "__other__";

const HOLIDAY_OPTIONS = [
  ...INDIAN_HOLIDAYS.map((holiday) => ({ value: holiday.name, label: holiday.name, hint: holiday.hint })),
  { value: OTHER_HOLIDAY, label: "Other…", hint: "Type a holiday name that is not listed" },
];

/** The next occurrence of a fixed month/day, today included, as yyyy-mm-dd. */
function nextOccurrence([month, day]: [number, number]): string {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  let year = now.getFullYear();
  if (new Date(year, month - 1, day) < today) year += 1;
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/**
 * Government holidays. A date added here is a holiday for every employee in
 * every branch: attendance marks it "H", nobody is absent for it, and payroll
 * deducts nothing for the day.
 *
 * The circulated 2026 list is built in and shown read-only underneath. It
 * covers 2026 only — from 2027 every holiday is declared here.
 */
function HolidaysPanel({ onActivity }: Props) {
  const [holidays, setHolidays] = useState<GovernmentHoliday[]>([]);
  const [builtin, setBuiltin] = useState<BuiltinHoliday[]>([]);
  const [day, setDay] = useState("");
  const [choice, setChoice] = useState("");
  const [customName, setCustomName] = useState("");
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const name = choice === OTHER_HOLIDAY ? customName.trim() : choice;

  useEffect(() => {
    let live = true;
    fetchHolidays()
      .then((result) => {
        if (!live) return;
        setHolidays(result.items);
        setBuiltin(result.builtin ?? []);
      })
      .catch((err) => { if (live) setError(err instanceof Error ? err.message : String(err)); })
      .finally(() => { if (live) setLoading(false); });
    return () => {
      live = false;
    };
  }, []);

  /** Picking a fixed-date holiday fills in its next date, unless one is already chosen. */
  const chooseName = (value: string) => {
    setChoice(value);
    const fixed = INDIAN_HOLIDAYS.find((holiday) => holiday.name === value)?.fixed;
    if (fixed && !day) setDay(nextOccurrence(fixed));
  };

  const clash = day ? holidays.find((holiday) => holiday.date === day) ?? null : null;

  const add = async () => {
    if (!day || !name || saving || clash) return;
    setSaving(true);
    setError(null);
    try {
      const result = await createHoliday(day, name);
      setHolidays(result.items);
      onActivity?.(`${result.holiday.name} declared a holiday`, "success");
      setDay("");
      setChoice("");
      setCustomName("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the holiday");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (holiday: GovernmentHoliday) => {
    if (!window.confirm(`Remove ${holiday.name} (${formatHolidayDate(holiday.date)}) as a holiday?`)) return;
    setError(null);
    try {
      const result = await deleteHoliday(holiday.id);
      setHolidays(result.items);
      onActivity?.(`${holiday.name} removed from holidays`, "success");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the holiday");
    }
  };

  return (
    <section className="db-card dm-panel">
      <div className="db-card-head dm-panel-head">
        <div className="dm-panel-title">
          <CalendarDays size={16} />
          <div>
            <h3 className="db-card-title">Government holidays</h3>
            <p>
              A date added here is a holiday for every employee. No attendance is needed that day
              and payroll is unchanged — nobody is marked absent or loses pay for it.
            </p>
          </div>
        </div>
      </div>
      <div className="db-card-body">
        <div className={`dm-holiday-add ${choice === OTHER_HOLIDAY ? "has-other" : ""}`}>
          <div className="dm-holiday-field">
            <span>Holiday</span>
            <Select
              value={choice}
              options={HOLIDAY_OPTIONS}
              onChange={chooseName}
              placeholder="Choose a holiday"
              ariaLabel="Holiday name"
              disabled={saving}
            />
          </div>
          {choice === OTHER_HOLIDAY && (
            <label className="dm-holiday-field">
              <span>Holiday name</span>
              <input
                className="modal-input"
                value={customName}
                onChange={(event) => setCustomName(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Enter") void add(); }}
                placeholder="Example: Local body election"
                maxLength={120}
                disabled={saving}
                autoFocus
              />
            </label>
          )}
          <div className="dm-holiday-field">
            <span>Date</span>
            <DatePicker
              value={day}
              onChange={setDay}
              placeholder="Pick the holiday date"
              ariaLabel="Holiday date"
              disabled={saving}
            />
          </div>
          <button
            type="button"
            className="db-btn is-primary"
            onClick={() => void add()}
            disabled={!day || !name || saving || Boolean(clash)}
          >
            <Plus size={14} /> {saving ? "Adding…" : "Declare holiday"}
          </button>
        </div>
        {clash && (
          <p className="dm-holiday-note" role="status">
            {formatHolidayDate(clash.date)} is already declared as {clash.name}.
          </p>
        )}
        {error && <p className="sh-form-error" role="alert">{error}</p>}

        <h4 className="dm-holiday-heading">Declared holidays</h4>
        {loading ? null : holidays.length === 0 ? (
          <div className="ds-empty-state dm-empty">
            <CalendarDays size={28} />
            <h3>No government holidays declared yet</h3>
            <p>From 2027 onwards, every holiday is declared here.</p>
          </div>
        ) : (
          <div className="dm-table-frame">
            <table className="dm-table is-register">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Holiday</th>
                  <th className="is-actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {holidays.map((holiday) => (
                  <tr key={holiday.id}>
                    <td><span className="dm-primary-cell"><strong>{formatHolidayDate(holiday.date)}</strong></span></td>
                    <td>{holiday.name}</td>
                    <td className="is-actions">
                      <button
                        type="button"
                        className="db-btn is-danger"
                        onClick={() => void remove(holiday)}
                        aria-label={`Remove ${holiday.name} as a holiday`}
                      >
                        <Trash2 size={13} /> Remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {builtin.length > 0 && (
          <>
            <h4 className="dm-holiday-heading">
              2026 holiday list <span className="dm-pill is-off">built in · 2026 only</span>
            </h4>
            <div className="dm-table-frame">
              <table className="dm-table is-register">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Holiday</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {builtin.map((holiday) => (
                    <tr key={holiday.date}>
                      <td><span className="dm-primary-cell"><strong>{formatHolidayDate(holiday.date)}</strong></span></td>
                      <td>{holiday.name}</td>
                      <td>
                        {holiday.applied ? (
                          <span className="dm-pill">Applied automatically</span>
                        ) : (
                          <span className="dm-pill is-off" title="Months before October 2026 were settled by hand">
                            Settled by hand
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
