// Git pins (GP): a person connects a public Amazon S3 bucket from a workspace's sources
// with the S3 form and saves it. With clio-schemas 0.7.0 the service refused the save
// ("unknown provider"); with the s3 provider it saves and the source is listed.
//
//   GP_SESSION  "<workspace id>/<session id>"
//   GP_ROOT     bucket and prefix (default: the ERA5 pressure-level prefix of October 2024)
//   GP_REGION   region (default us-west-2)
//   GP_NAME     the source's name (default "ERA5 pressure levels <prefix month>")
//
// Prints one JSON line: whether the form opened, any alert text, and the workspace's S3
// sources after saving.
import { api } from "./_lib.mjs";

const settle = (page, ms = 1500) => page.waitForTimeout(ms);

export default async function (page, shot) {
  const [workspace, session] = process.env.GP_SESSION.split("/");
  const root = process.env.GP_ROOT || "nsf-ncar-era5/e5.oper.an.pl/202410";
  const region = process.env.GP_REGION || "us-west-2";
  const name = process.env.GP_NAME || `ERA5 pressure levels ${root.split("/").pop()}`;
  await page.goto(new URL(`/workspaces/${workspace}/sessions/${session}`, page.url()).toString());
  await settle(page, 6000);
  const report = { root };
  let connect = page.getByRole("button", { name: "Connect a data source" }).filter({ visible: true });
  // The workspace canvas's Sources tab holds "Connect a data source".
  for (const label of ["Open workspace canvas", "Sources"]) {
    if (await connect.count()) break;
    const button = page.getByRole("button", { name: label, exact: true }).filter({ visible: true });
    if (await button.count()) {
      await button.last().click();
      await settle(page);
    }
    connect = page.getByRole("button", { name: "Connect a data source" }).filter({ visible: true });
  }
  await shot("workspace");
  await connect.first().click({ timeout: 15000 });
  await settle(page);
  await shot("picker");
  // The form's Source picker: one choice per provider, named by the provider.
  await page.locator('[role="dialog"]').getByRole("radio", { name: "Amazon S3" })
    .or(page.locator('[role="dialog"]').getByRole("button", { name: "Amazon S3" }))
    .first().click();
  await settle(page);
  report.formOpened = await page.getByLabel("Bucket and prefix").count() > 0;
  await page.getByLabel("Bucket and prefix").fill(root);
  await page.getByLabel("Region").fill(region);
  await page.getByLabel("Name").fill(name).catch(() => {});
  await shot("form");
  await page.locator('[role="dialog"]').getByRole("button", { name: "Connect", exact: true }).last().click();
  await settle(page, 6000);
  report.alerts = await page.getByRole("alert").filter({ visible: true }).allInnerTexts();
  await shot("saved");
  const listed = await api("GET", `/v1/workspaces/${workspace}/sources`);
  report.s3 = (listed.sources ?? listed)
    .filter((row) => row.provider === "s3")
    .map((row) => ({ label: row.label, root: row.root, mode: row.mode }));
  console.log(JSON.stringify(report));
}
