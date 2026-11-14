// Final gates (GF): the PDF viewer in one browser session. A plain request opens the paper
// (more_conn_answer.mjs: MC_SESSION, MC_SEND, MC_ADD=1), the person allows the view's
// reads for the session as the permission card offers, then pages through it
// (vf_pdf_pages.mjs: VF_SESSION, VF_PAGES, VF_MOUNTS, VF_PHONE). Keeping one browser keeps
// the view mounted between the two steps.
import { allowPending } from "./_lib.mjs";
import answer from "./more_conn_answer.mjs";
import pages from "./vf_pdf_pages.mjs";

export default async function (page, shot) {
  await answer(page, shot);
  const session = process.env.MC_SESSION.split("/")[1];
  const log = [];
  for (let i = 0; i < 10; i += 1) {
    await allowPending(session, log);
    await page.waitForTimeout(2000);
  }
  console.log("GF_PDF_ALLOWED " + JSON.stringify(log));
  process.env.VF_SESSION = process.env.VF_SESSION || process.env.MC_SESSION;
  await pages(page, shot);
}
