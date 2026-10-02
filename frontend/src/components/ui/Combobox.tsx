"use client";

import { useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";
import { usePopover } from "./usePopover";

interface ComboboxProps {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  id?: string;
  ariaLabel: string;
  placeholder?: string;
  disabled?: boolean;
}

/** Editable suggestions: values outside the list remain valid. */
export default function Combobox({ value, options, onChange, id, ariaLabel, placeholder, disabled }: ComboboxProps) {
  const { open, up, anchorRef, panelRef, style, setOpen, close } = usePopover<HTMLInputElement>();
  const listId = `${useId()}-suggestions`;
  const [active, setActive] = useState(-1);
  const matches = options.filter((option) => option.toLowerCase().includes(value.trim().toLowerCase()));
  useEffect(() => {
    if (open) panelRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, active, panelRef]);
  const pick = (option: string) => { onChange(option); close(); };
  return <div className="ui-combobox">
    <input id={id} ref={anchorRef} className="ui-combobox-input modal-input" value={value} placeholder={placeholder} disabled={disabled} autoComplete="off" role="combobox" aria-label={ariaLabel} aria-autocomplete="list" aria-expanded={open} aria-controls={listId} aria-activedescendant={open && active >= 0 && matches[active] ? `${listId}-${active}` : undefined}
      onFocus={() => { setActive(-1); setOpen(true); }}
      onChange={(event) => { onChange(event.target.value); setActive(-1); setOpen(true); }}
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          setOpen(true);
          setActive((current) => {
            if (!matches.length) return -1;
            const delta = event.key === "ArrowDown" ? 1 : -1;
            if (current < 0 || current >= matches.length) return delta === 1 ? 0 : matches.length - 1;
            return (current + delta + matches.length) % matches.length;
          });
        }
        else if (open && event.key === "Enter") { event.preventDefault(); if (matches[active]) pick(matches[active]); else close(); }
        else if (event.key === "Tab") close(false);
      }} />
    <ChevronDown size={15} className="ui-combobox-caret" aria-hidden="true" />
    {open && createPortal(<div ref={panelRef} id={listId} className={`ui-pop ui-select-panel ${up ? "is-up" : ""}`} style={style} role="listbox" aria-label={`${ariaLabel} suggestions`}>
      <div className="ui-select-list">{matches.map((option, index) => <button id={`${listId}-${index}`} key={option} type="button" role="option" aria-selected={value === option} tabIndex={-1} data-index={index} className={`ui-select-option ${index === active ? "is-active" : ""}`} onMouseDown={(event) => event.preventDefault()} onMouseMove={() => setActive(index)} onClick={() => pick(option)}><span className="ui-select-option-text">{option}</span>{value === option && <Check size={14} />}</button>)}{!matches.length && <p className="ui-select-none">{value ? "Your entered country will be used." : "Type a country name."}</p>}</div>
    </div>, document.body)}
  </div>;
}
