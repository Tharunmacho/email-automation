/* eslint-disable @next/next/no-img-element -- the supplied brand files must be served byte-for-byte, without image optimisation. */

interface BrandLogoProps {
  className?: string;
  /** The surrounding control already supplies an accessible name when true. */
  decorative?: boolean;
}

/**
 * The supplied Adira artwork, aligned as a responsive lockup.
 *
 * Each image is used unchanged. CSS only clips the transparent canvas around
 * it and aligns the three original pieces; light and dark select their own
 * supplied artwork rather than recolouring or recreating the logo.
 */
export default function BrandLogo({ className = "", decorative = false }: BrandLogoProps) {
  return (
    <span
      className={`brand-lockup ${className}`.trim()}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : "Adira Enterprises — We find, you shine"}
      aria-hidden={decorative || undefined}
    >
      <span className="brand-piece brand-mark" aria-hidden="true">
        <img className="brand-art is-light" src="/adira-mark-light-original.png" alt="" width="1920" height="1080" draggable="false" />
        <img className="brand-art is-dark" src="/adira-mark-dark-original.png" alt="" width="1920" height="1080" draggable="false" />
      </span>

      <span className="brand-copy" aria-hidden="true">
        <span className="brand-piece brand-name">
          <img className="brand-art is-light" src="/adira-word-light-original.png" alt="" width="1920" height="1080" draggable="false" />
          <img className="brand-art is-dark" src="/adira-word-dark-original.png" alt="" width="1920" height="1080" draggable="false" />
        </span>
        <span className="brand-piece brand-tagline">
          <img className="brand-art is-light" src="/adira-tagline-light-original.png" alt="" width="1920" height="1080" draggable="false" />
          <img className="brand-art is-dark" src="/adira-tagline-dark-original.png" alt="" width="1920" height="1080" draggable="false" />
        </span>
      </span>
    </span>
  );
}
