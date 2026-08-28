"use client";

import React, { useMemo, useState } from "react";
import { ArrowLeft, CheckCircle2, Plus, Save, Trash2 } from "lucide-react";

import {
  editableToProfile,
  renameExtra,
  toEditableState,
  type EditableEdu,
  type EditableJobAnswer,
  type EditableProject,
  type EditableState,
  type EditableWorkExp,
} from "@/lib/candidateProfile";
import type { CandidateProfile, CandidateRecord } from "@/lib/api";
import { initialsOf } from "@/lib/format";

interface CandidateEditScreenProps {
  candidate: CandidateRecord;
  mode?: "edit" | "create";
  saving: boolean;
  error?: string | null;
  verifying?: boolean;
  onBack: () => void;
  onSave: (candidateId: string, profile: CandidateProfile) => void;
  /**
   * Sign the record off without leaving the editor.
   *
   * Correcting what the parser got wrong and then declaring the result correct
   * is one job, and it used to take two screens: save here, go back, open the
   * executive view, verify there.
   */
  onVerify?: (candidateId: string) => void;
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="cedit-field">
      <span className="cedit-label">
        {label}
        {hint && <span className="cedit-hint">{hint}</span>}
      </span>
      {children}
    </label>
  );
}

function SectionCard({
  title,
  onAdd,
  addLabel,
  children,
}: {
  title: string;
  onAdd?: () => void;
  addLabel?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="cedit-card">
      <div className="cedit-card-head">
        <h3 className="cedit-card-title">{title}</h3>
        {onAdd && (
          <button type="button" className="cedit-add" onClick={onAdd}>
            <Plus size={14} /> {addLabel ?? "Add"}
          </button>
        )}
      </div>
      {children}
    </section>
  );
}

/**
 * The editor, and only the editor.
 *
 * No executive rendering happens here — every value on this screen is a control
 * the user can change. Keeping the reader out of the editor is what makes the
 * form scannable: there is exactly one column of inputs, and nothing on screen
 * that merely restates what an input already holds.
 */
export default function CandidateEditScreen({
  candidate,
  mode = "edit",
  saving,
  error = null,
  verifying = false,
  onBack,
  onSave,
  onVerify,
}: CandidateEditScreenProps) {
  const profile = candidate.profile ?? {};
  const isCreating = mode === "create";
  const isVerified = candidate.status === "verified";

  // Seeded once per record. Re-deriving on every poll would overwrite whatever
  // the user is halfway through typing, since the list refreshes every 5s.
  const initial = useMemo(
    () => toEditableState(profile, candidate),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [candidate.id],
  );
  const [state, setState] = useState<EditableState>(initial);

  const dirty = useMemo(() => JSON.stringify(state) !== JSON.stringify(initial), [state, initial]);

  const set = <K extends keyof EditableState>(key: K, value: EditableState[K]) =>
    setState((prev) => ({ ...prev, [key]: value }));

  const text = (key: keyof EditableState) => (
    event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => setState((prev) => ({ ...prev, [key]: event.target.value }));

  // ---- repeatable rows --------------------------------------------------- //
  const updateAt = <T,>(list: T[], index: number, patch: Partial<T>): T[] =>
    list.map((item, i) => (i === index ? { ...item, ...patch } : item));

  const removeAt = <T,>(list: T[], index: number): T[] => list.filter((_, i) => i !== index);

  const updateExp = (index: number, patch: Partial<EditableWorkExp>) =>
    set("work_experience", updateAt(state.work_experience, index, patch));

  const updateEdu = (index: number, patch: Partial<EditableEdu>) =>
    set("education", updateAt(state.education, index, patch));

  const updateProj = (index: number, patch: Partial<EditableProject>) =>
    set("projects", updateAt(state.projects, index, patch));

  const updateAnswer = (index: number, patch: Partial<EditableJobAnswer>) =>
    set("job_answers", updateAt(state.job_answers, index, patch));

  const handleBack = () => {
    if (dirty && !confirm("Discard the unsaved changes on this profile?")) return;
    onBack();
  };

  const handleSave = () => {
    if (isCreating && !state.full_name.trim()) return;
    onSave(candidate.id, editableToProfile(profile, state));
  };

  return (
    <div className="cscreen" style={{ animation: "fadeIn 0.3s ease" }}>
      <div className="cscreen-topbar">
        <button type="button" className="jod-back" onClick={handleBack} title="Back to the candidates list">
          <ArrowLeft size={15} /> Back to candidates
        </button>

        <div className="cscreen-topbar-actions">
          {dirty && <span className="cedit-dirty">Unsaved changes</span>}

          {/* Held shut while the form is dirty: verifying is a statement about
              the stored record, and what is on screen is not it yet. */}
          {/* Shown when onVerify is passed; hidden in StaffScreen view */}
          {!isCreating && onVerify && (
            <button
              type="button"
              className={`cscreen-btn ${isVerified ? "is-verified" : ""}`}
              onClick={() => onVerify(candidate.id)}
              disabled={verifying || saving || isVerified || dirty}
              title={
                dirty
                  ? "Save the changes first — verifying signs off the stored record."
                  : "Mark this profile as verified"
              }
            >
              <CheckCircle2 size={15} />
              {verifying ? "Verifying…" : isVerified ? "Verified profile" : "Verify profile"}
            </button>
          )}

          <button
            type="button"
            className="cscreen-btn is-primary"
            onClick={handleSave}
            disabled={saving || (isCreating && !state.full_name.trim())}
          >
            <Save size={15} /> {saving ? "Saving…" : isCreating ? "Add candidate" : "Save changes"}
          </button>
        </div>
      </div>

      <header className="cedit-hero">
        <span className="cprof-monogram" aria-hidden="true">
          {initialsOf(state.full_name)}
        </span>
        <div>
          <h2 className="cprof-name">
            {isCreating ? "Add a candidate" : `Editing ${state.full_name || "candidate"}`}
          </h2>
          <p className="cprof-meta">
            {isCreating
              ? "Enter the candidate's details manually. Nothing is stored until you add them."
              : "Changes are written to MongoDB Atlas when you save. Nothing is stored until then."}
          </p>
        </div>
      </header>

      {error && <div className="cscreen-error" role="alert">{error}</div>}

      <div className="cedit-sections">
        <SectionCard title="Identity and contact">
          <div className="cedit-grid">
            <Field label="Full name" hint={isCreating ? "required" : undefined}>
              <input
                className="cedit-input"
                type="text"
                value={state.full_name}
                onChange={text("full_name")}
                required={isCreating}
                autoFocus={isCreating}
              />
            </Field>
            <Field label="Current designation">
              <input className="cedit-input" type="text" value={state.designation} onChange={text("designation")} />
            </Field>
            <Field label="Email">
              <input className="cedit-input" type="text" value={state.email} onChange={text("email")} />
            </Field>
            <Field label="Phone">
              <input className="cedit-input" type="text" value={state.phone} onChange={text("phone")} />
            </Field>
            <Field label="Address / location">
              <input className="cedit-input" type="text" value={state.location} onChange={text("location")} />
            </Field>
            <Field label="Total experience" hint="in years">
              <input
                className="cedit-input"
                type="number"
                min="0"
                step="0.1"
                value={state.experience}
                onChange={text("experience")}
              />
            </Field>
            <Field label="Languages" hint="comma separated">
              <input className="cedit-input" type="text" value={state.languages} onChange={text("languages")} />
            </Field>
            <Field label="LinkedIn URL">
              <input className="cedit-input" type="text" value={state.linkedin} onChange={text("linkedin")} />
            </Field>
            <Field label="GitHub URL">
              <input className="cedit-input" type="text" value={state.github} onChange={text("github")} />
            </Field>
          </div>
        </SectionCard>

        {/* What they applied for, kept apart from who they are.

            The WhatsApp bot fills this in by asking; an email candidate arrives
            with it empty, and it is filled in here once a recruiter has spoken
            to them. `job_id` is deliberately absent from this form: it is the
            key the CV rules and the bot's own records point at, and retyping a
            title must not silently repoint the record at a different job. */}
        <SectionCard title="Job and preferences">
          <div className="cedit-grid">
            <Field label="Job applied for">
              <input
                className="cedit-input"
                type="text"
                value={state.job_title}
                placeholder="Electrician"
                onChange={text("job_title")}
              />
            </Field>
            <Field label="Course / trade" hint="qualification">
              <input
                className="cedit-input"
                type="text"
                value={state.course_or_trade}
                placeholder="ITI Electrician"
                onChange={text("course_or_trade")}
              />
            </Field>
            <Field label="Destination country" hint="where they want to work">
              <input
                className="cedit-input"
                type="text"
                value={state.destination_country}
                placeholder="Singapore"
                onChange={text("destination_country")}
              />
            </Field>
            <Field label="State / district preference">
              <input
                className="cedit-input"
                type="text"
                value={state.state_preference}
                onChange={text("state_preference")}
              />
            </Field>
            <Field label="Job preference" hint="in their own words">
              <input
                className="cedit-input"
                type="text"
                value={state.job_preference}
                onChange={text("job_preference")}
              />
            </Field>
            <Field label="Available to join" hint="free text">
              <input
                className="cedit-input"
                type="text"
                value={state.available_from}
                placeholder="Immediately / after 2 months"
                onChange={text("available_from")}
              />
            </Field>
          </div>

          <Field label="Trade skills" hint="comma separated">
            <input
              className="cedit-input"
              type="text"
              value={state.trade_skills}
              placeholder="EOT Crane, TIG Welding"
              onChange={text("trade_skills")}
            />
          </Field>

          <p className="cedit-note">
            The destination country is what the CV policy reads. Keep it to one country &mdash;
            &ldquo;Singapore&rdquo;, never &ldquo;GCC&rdquo; or a pair.
          </p>
        </SectionCard>

        <SectionCard
          title="Screening questions"
          addLabel="Add question"
          onAdd={() =>
            set("job_answers", [
              ...state.job_answers,
              { question_id: "", question: "", answer: "", kind: "text" },
            ])
          }
        >
          {state.job_answers.length === 0 ? (
            <p className="cedit-note">
              No screening answers recorded. These arrive with a WhatsApp registration; add one
              here to record an answer given over the phone.
            </p>
          ) : (
            <div className="cedit-rows">
              {state.job_answers.map((entry, index) => (
                <div key={entry.question_id || index} className="cedit-row">
                  <div className="cedit-row-head">
                    <span className="cedit-row-title">Question #{index + 1}</span>
                    <button
                      type="button"
                      className="cedit-remove"
                      aria-label={`Remove question ${index + 1}`}
                      onClick={() => set("job_answers", removeAt(state.job_answers, index))}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <Field label="Question">
                    <input
                      className="cedit-input"
                      type="text"
                      value={entry.question}
                      onChange={(e) => updateAnswer(index, { question: e.target.value })}
                    />
                  </Field>
                  <Field label="Answer">
                    <textarea
                      className="cedit-input cedit-textarea"
                      rows={2}
                      value={entry.answer}
                      onChange={(e) => updateAnswer(index, { answer: e.target.value })}
                    />
                  </Field>
                </div>
              ))}
            </div>
          )}
          <p className="cedit-note">
            The wording is stored with the answer, not looked up. Editing a question here changes
            what this candidate is recorded as having been asked &mdash; it does not change the
            question the bot puts to anyone else.
          </p>
        </SectionCard>

        <SectionCard title="Summary">
          <textarea
            className="cedit-input cedit-textarea"
            rows={4}
            value={state.summary}
            onChange={text("summary")}
            placeholder="A short profile summary…"
          />
        </SectionCard>

        <SectionCard title="Skills">
          <textarea
            className="cedit-input cedit-textarea"
            rows={3}
            value={state.skills}
            onChange={text("skills")}
            placeholder="React, TypeScript, Python…"
          />
          <p className="cedit-note">One comma-separated list. Each item becomes its own skill.</p>
        </SectionCard>

        <SectionCard
          title="Experience"
          addLabel="Add experience"
          onAdd={() =>
            set("work_experience", [
              ...state.work_experience,
              { company: "", designation: "", start_date: "", end_date: "", location: "", description: "" },
            ])
          }
        >
          {state.work_experience.length === 0 ? (
            <p className="cedit-note">No experience recorded. Use “Add experience” to enter one.</p>
          ) : (
            <div className="cedit-rows">
              {state.work_experience.map((exp, index) => (
                <div key={index} className="cedit-row">
                  <div className="cedit-row-head">
                    <span className="cedit-row-title">Experience #{index + 1}</span>
                    <button
                      type="button"
                      className="cedit-remove"
                      aria-label={`Remove experience ${index + 1}`}
                      onClick={() => set("work_experience", removeAt(state.work_experience, index))}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="cedit-grid">
                    <Field label="Designation">
                      <input
                        className="cedit-input"
                        type="text"
                        value={exp.designation}
                        onChange={(e) => updateExp(index, { designation: e.target.value })}
                      />
                    </Field>
                    <Field label="Company">
                      <input
                        className="cedit-input"
                        type="text"
                        value={exp.company}
                        onChange={(e) => updateExp(index, { company: e.target.value })}
                      />
                    </Field>
                    <Field label="Start date">
                      <input
                        className="cedit-input"
                        type="text"
                        value={exp.start_date}
                        placeholder="Jan 2022"
                        onChange={(e) => updateExp(index, { start_date: e.target.value })}
                      />
                    </Field>
                    <Field label="End date">
                      <input
                        className="cedit-input"
                        type="text"
                        value={exp.end_date}
                        placeholder="Present"
                        onChange={(e) => updateExp(index, { end_date: e.target.value })}
                      />
                    </Field>
                    <Field label="Location">
                      <input
                        className="cedit-input"
                        type="text"
                        value={exp.location}
                        onChange={(e) => updateExp(index, { location: e.target.value })}
                      />
                    </Field>
                  </div>
                  <Field label="Description">
                    <textarea
                      className="cedit-input cedit-textarea"
                      rows={3}
                      value={exp.description}
                      onChange={(e) => updateExp(index, { description: e.target.value })}
                    />
                  </Field>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Education"
          addLabel="Add education"
          onAdd={() =>
            set("education", [
              ...state.education,
              { degree: "", institution: "", start_date: "", end_date: "", grade: "" },
            ])
          }
        >
          {state.education.length === 0 ? (
            <p className="cedit-note">No education recorded. Use “Add education” to enter one.</p>
          ) : (
            <div className="cedit-rows">
              {state.education.map((edu, index) => (
                <div key={index} className="cedit-row">
                  <div className="cedit-row-head">
                    <span className="cedit-row-title">Education #{index + 1}</span>
                    <button
                      type="button"
                      className="cedit-remove"
                      aria-label={`Remove education ${index + 1}`}
                      onClick={() => set("education", removeAt(state.education, index))}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="cedit-grid">
                    <Field label="Degree / qualification">
                      <input
                        className="cedit-input"
                        type="text"
                        value={edu.degree}
                        onChange={(e) => updateEdu(index, { degree: e.target.value })}
                      />
                    </Field>
                    <Field label="Institution">
                      <input
                        className="cedit-input"
                        type="text"
                        value={edu.institution}
                        onChange={(e) => updateEdu(index, { institution: e.target.value })}
                      />
                    </Field>
                    <Field label="Start date">
                      <input
                        className="cedit-input"
                        type="text"
                        value={edu.start_date}
                        onChange={(e) => updateEdu(index, { start_date: e.target.value })}
                      />
                    </Field>
                    <Field label="End date">
                      <input
                        className="cedit-input"
                        type="text"
                        value={edu.end_date}
                        onChange={(e) => updateEdu(index, { end_date: e.target.value })}
                      />
                    </Field>
                    <Field label="Grade">
                      <input
                        className="cedit-input"
                        type="text"
                        value={edu.grade}
                        onChange={(e) => updateEdu(index, { grade: e.target.value })}
                      />
                    </Field>
                  </div>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Projects"
          addLabel="Add project"
          onAdd={() => set("projects", [...state.projects, { name: "", description: "", technologies: "" }])}
        >
          {state.projects.length === 0 ? (
            <p className="cedit-note">No projects recorded. Use “Add project” to enter one.</p>
          ) : (
            <div className="cedit-rows">
              {state.projects.map((project, index) => (
                <div key={index} className="cedit-row">
                  <div className="cedit-row-head">
                    <span className="cedit-row-title">Project #{index + 1}</span>
                    <button
                      type="button"
                      className="cedit-remove"
                      aria-label={`Remove project ${index + 1}`}
                      onClick={() => set("projects", removeAt(state.projects, index))}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  <div className="cedit-grid">
                    <Field label="Project name">
                      <input
                        className="cedit-input"
                        type="text"
                        value={project.name}
                        onChange={(e) => updateProj(index, { name: e.target.value })}
                      />
                    </Field>
                    <Field label="Technologies" hint="comma separated">
                      <input
                        className="cedit-input"
                        type="text"
                        value={project.technologies}
                        onChange={(e) => updateProj(index, { technologies: e.target.value })}
                      />
                    </Field>
                  </div>
                  <Field label="Description">
                    <textarea
                      className="cedit-input cedit-textarea"
                      rows={3}
                      value={project.description}
                      onChange={(e) => updateProj(index, { description: e.target.value })}
                    />
                  </Field>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Achievements"
          addLabel="Add item"
          onAdd={() => set("achievements", [...state.achievements, ""])}
        >
          {state.achievements.length === 0 ? (
            <p className="cedit-note">No achievements recorded.</p>
          ) : (
            <div className="cedit-list">
              {state.achievements.map((item, index) => (
                <div key={index} className="cedit-list-row">
                  <input
                    className="cedit-input"
                    type="text"
                    value={item}
                    aria-label={`Achievement ${index + 1}`}
                    onChange={(e) =>
                      set(
                        "achievements",
                        state.achievements.map((a, i) => (i === index ? e.target.value : a)),
                      )
                    }
                  />
                  <button
                    type="button"
                    className="cedit-remove"
                    aria-label={`Remove achievement ${index + 1}`}
                    onClick={() => set("achievements", removeAt(state.achievements, index))}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        <SectionCard
          title="Certifications"
          addLabel="Add item"
          onAdd={() => set("certifications", [...state.certifications, ""])}
        >
          {state.certifications.length === 0 ? (
            <p className="cedit-note">No certifications recorded.</p>
          ) : (
            <div className="cedit-list">
              {state.certifications.map((item, index) => (
                <div key={index} className="cedit-list-row">
                  <input
                    className="cedit-input"
                    type="text"
                    value={item}
                    aria-label={`Certification ${index + 1}`}
                    onChange={(e) =>
                      set(
                        "certifications",
                        state.certifications.map((c, i) => (i === index ? e.target.value : c)),
                      )
                    }
                  />
                  <button
                    type="button"
                    className="cedit-remove"
                    aria-label={`Remove certification ${index + 1}`}
                    onClick={() => set("certifications", removeAt(state.certifications, index))}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </SectionCard>

        {/* Whatever the extractor returned that the fixed schema has no field
            for. The form is built from the data, so a resume carrying a field
            nobody anticipated is editable without a code change, and an edit
            goes back to the same path inside additional_info. */}
        <SectionCard
          title="Additional fields"
          addLabel="Add field"
          onAdd={() => set("extras", [...state.extras, { path: "", label: "", value: "", kind: "text" }])}
        >
          {state.extras.length === 0 ? (
            <p className="cedit-note">
              Nothing extra was extracted. Use “Add field” to record something of your own.
            </p>
          ) : (
            <div className="cedit-list">
              {state.extras.map((extra, index) => (
                <div key={`${extra.path}-${index}`} className="cedit-extra-row">
                  <input
                    className="cedit-input"
                    type="text"
                    value={extra.label}
                    placeholder="Field name"
                    aria-label={`Field name ${index + 1}`}
                    onChange={(e) =>
                      set(
                        "extras",
                        state.extras.map((x, i) => (i === index ? renameExtra(x, e.target.value) : x)),
                      )
                    }
                  />
                  <input
                    className="cedit-input"
                    type="text"
                    value={extra.value}
                    placeholder={extra.kind === "list" ? "Comma separated values" : "Value"}
                    aria-label={`Field value ${index + 1}`}
                    onChange={(e) =>
                      set(
                        "extras",
                        state.extras.map((x, i) => (i === index ? { ...x, value: e.target.value } : x)),
                      )
                    }
                  />
                  <button
                    type="button"
                    className="cedit-remove"
                    aria-label={`Remove field ${index + 1}`}
                    onClick={() => set("extras", removeAt(state.extras, index))}
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <p className="cedit-note">Clearing a value removes the field when you save.</p>
        </SectionCard>
      </div>

      <div className="cedit-footer">
        <span className="cedit-footer-note">
          {isCreating
            ? "The profile will be added to the candidate pool when you save."
            : dirty ? "You have unsaved changes." : "Everything on this screen is saved."}
        </span>
        <div className="cscreen-topbar-actions">
          <button type="button" className="cscreen-btn" onClick={handleBack}>
            Cancel
          </button>
          <button
            type="button"
            className="cscreen-btn is-primary"
            onClick={handleSave}
            disabled={saving || (isCreating && !state.full_name.trim())}
          >
            <Save size={15} /> {saving ? "Saving…" : isCreating ? "Add candidate" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}
