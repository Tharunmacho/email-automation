import type { StaticImageData } from "next/image";

import mobileSmallWebp from "./mobile-640.webp";
import mobileLargeWebp from "./mobile-1360.webp";
import panelSmallWebp from "./panel-640.webp";
import panelLargeWebp from "./panel-1285.webp";
import portraitSmallWebp from "./portrait-640.webp";
import portraitLargeWebp from "./portrait-1122.webp";

const srcSet = (small: StaticImageData, large: StaticImageData) =>
  `${small.src} ${small.width}w, ${large.src} ${large.width}w`;

// The artwork fills both dimensions. Include height when selecting resolution
// so a tall panel cannot upscale the smaller variant and soften its edges.
const desktopSizes = (image: StaticImageData) =>
  `max(calc(50vw - 8px), calc((100vh - 16px) * ${image.width / image.height}))`;

// Static imports give each encoded asset a content hash and immutable caching.
// Keep mobile first so short phone banners retain their own composition.
export const loginArtworkSources = [
  {
    media: "(max-width: 680px)",
    type: "image/webp",
    sizes: "calc(100vw - 16px)",
    srcSet: srcSet(mobileSmallWebp, mobileLargeWebp),
    width: mobileLargeWebp.width,
    height: mobileLargeWebp.height,
  },
  {
    media: "(min-aspect-ratio: 9/5)",
    type: "image/webp",
    sizes: desktopSizes(panelLargeWebp),
    srcSet: srcSet(panelSmallWebp, panelLargeWebp),
    width: panelLargeWebp.width,
    height: panelLargeWebp.height,
  },
  {
    media: undefined,
    type: "image/webp",
    sizes: desktopSizes(portraitLargeWebp),
    srcSet: srcSet(portraitSmallWebp, portraitLargeWebp),
    width: portraitLargeWebp.width,
    height: portraitLargeWebp.height,
  },
];

export const loginArtworkFallback = portraitLargeWebp;
