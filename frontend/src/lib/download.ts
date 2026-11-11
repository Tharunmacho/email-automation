/**
 * Download a protected file without leaving the page.
 *
 * The request deliberately carries **no `Authorization` header**. That header
 * is what made this a "non-simple" cross-origin request, and a non-simple
 * request needs a CORS preflight — which is precisely what was being dropped
 * in the field, producing "Failed to fetch" with nothing at all in the server
 * log. Authorisation instead rides in the URL's `token` query parameter, which
 * the API accepts (see `current_user`) and which `resumeDownloadUrl` and
 * `identityFileUrl` already put there. A plain GET with no custom headers is a
 * simple request: no preflight, nothing for an extension or proxy to refuse.
 *
 * The bytes then become a blob URL, which is same-origin, so the `download`
 * attribute is honoured and the file saves in place — no new tab, no
 * navigation away from the profile.
 */
export async function saveFile(url: string, fallbackName: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(url);
  } catch {
    // The request never became an HTTP exchange. Nothing here can say why, and
    // the file is very probably fine — so fall back to letting the browser
    // fetch it itself, in a hidden frame so the page still does not move.
    downloadInBackground(url);
    return;
  }

  if (!response.ok) {
    let detail = `Server replied ${response.status}`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // Not a JSON error body — the status line is what there is.
    }
    throw new Error(detail);
  }

  const objectUrl = window.URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = nameFromDisposition(response) || fallbackName;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Not revoked on this tick: the click is handled asynchronously, and tearing
  // the object URL down straight away cancels the save of a large file before
  // the browser has finished reading it.
  window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 60_000);
}

/**
 * Let the browser fetch a file itself, in a frame nobody sees.
 *
 * A response carrying `Content-Disposition: attachment` is downloaded rather
 * than rendered, so the frame stays blank and the page it sits on never moves.
 * The last resort, for when `fetch` itself is blocked.
 */
function downloadInBackground(url: string): void {
  const frame = document.createElement("iframe");
  frame.style.display = "none";
  frame.src = url;
  document.body.appendChild(frame);
  window.setTimeout(() => frame.remove(), 120_000);
}

/**
 * The filename the server chose, if it sent one.
 *
 * Worth reading rather than always naming the file here: for a document cut
 * out of an application bundle only the server knows which pages it took, and
 * `application_passport_p55.pdf` is the difference between four downloads a
 * recruiter can tell apart and four called `passport.pdf`.
 *
 * Readable only because the API lists `Content-Disposition` in its CORS
 * `expose_headers`; without that the browser hides it from JavaScript.
 */
function nameFromDisposition(response: Response): string | null {
  const header = response.headers.get("content-disposition") || "";
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (encoded) {
    try {
      return decodeURIComponent(encoded[1].trim());
    } catch {
      // A malformed extended filename can still have a usable plain fallback.
    }
  }
  const plain = /filename="([^"]+)"/.exec(header);
  return plain ? plain[1] : null;
}
