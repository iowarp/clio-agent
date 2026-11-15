// AQ13, AQ14, AQ23, AQ24 then AQ15 in one conversation: host_tab.mjs, then
// host_live_set.mjs with the panel still in its tab.
import hostTab from "./host_tab.mjs";
import hostLiveSet from "./host_live_set.mjs";

export default async function (page, shot) {
  await hostTab(page, shot);
  await hostLiveSet(page, shot);
}
