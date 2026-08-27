interface BrandLogoProps {
  className?: string;
  /** The surrounding control already supplies an accessible name when true. */
  decorative?: boolean;
}

/**
 * The responsive Adira lockup.
 *
 * Keeping the mark as inline vector geometry makes every instance sharp at its
 * actual size and lets the application theme recolour it without loading a
 * second image. The copy remains real text, so it is never softened by raster
 * scaling and the mark/copy alignment can be tuned as one unit.
 */
export default function BrandLogo({ className = "", decorative = false }: BrandLogoProps) {
  return (
    <span
      className={`brand-lockup ${className}`.trim()}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : "Adira Enterprises — We find, you shine"}
      aria-hidden={decorative || undefined}
    >
      <svg className="brand-mark" viewBox="0 0 120 120" aria-hidden="true" focusable="false">
        <defs>
          <mask id="adira-mark-cutout" maskUnits="userSpaceOnUse" x="0" y="0" width="120" height="120">
            <rect width="120" height="120" fill="white" />
            <circle cx="60" cy="49" r="13" fill="black" />
            <path d="M43 66h34L60 96z" fill="black" />
          </mask>
        </defs>
        <g mask="url(#adira-mark-cutout)">
          <path className="brand-mark-left" d="M60 4 5 112h43l12-21z" />
          <path className="brand-mark-right" d="M60 4v87l12 21h43z" />
        </g>
      </svg>

      <span className="brand-copy" aria-hidden="true">
        <span className="brand-name">adira enterprises</span>
        <span className="brand-tagline">WE FIND, YOU SHINE</span>
      </span>
    </span>
  );
}
