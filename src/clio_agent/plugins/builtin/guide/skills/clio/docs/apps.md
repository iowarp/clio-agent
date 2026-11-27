---
title: "Apps"
description: "See a connector's live view in the conversation, open it in a tab, show it to the agent, comment on it, link it to a table in a dashboard, and reopen it later."
source: https://clio.iowarp.ai/docs/apps/
---

# Apps

See a connector's live view in the conversation, open it in a tab, show it to the agent, comment on it, link it to a table in a dashboard, and reopen it later.

> **Note: Development build**
>
> Apps in tabs, comments on apps and apps in dashboards are in the development build.
> v0.9.6.6 does not have them. See [Features and versions](docs/versions.md).

Some tools answer with a live view instead of text: stations on a globe you can turn,
a paper you can page through, a ParaView scene you can rotate. CLIO calls these apps.
An app comes from a [connector](docs/connectors.md), a plugin or another MCP server, and
it follows the open MCP Apps standard, so the same app works in other hosts too.

The agent opens an app by calling one of its tools. You use the view directly, and the
agent sees what the view reports, can look at it, and can act on it.

## Walk-through: stations on a globe

This walk-through uses the Geo connector and a list of ten example GNSS stations
around Mount Rainier, `example_stations_rainier.csv`, in a workspace called
`cascadia`. Every step is a plain request or a click.

1. **Ask.** "Show the GNSS stations in example_stations_rainier.csv on a globe,
   coloured by station kind." If Geo is not in the conversation yet, the agent offers
   it first; choose **Add** (see
   [When the agent offers a connector or plugin](docs/connectors.md#when-the-agent-offers-a-connector-or-plugin)).
   The globe then appears in a card in the conversation, in the same reply.

   ![A conversation in the cascadia workspace with a card titled Geo: GNSS stations near Mount Rainier, by kind, marked Ready, with Open in tab; the card shows a map of Mount Rainier with ten labelled station dots, blue for continuous and orange for campaign, and a layer list with the colour key](plugin-asset://guide/captures/geo-card-inline.webp)
*The view in its card. The layer list names what is drawn and how it is coloured. Click a dot for the station's details.*

2. **Open it in a tab.** Choose **Open in tab** on the card. The view moves to a tab
   in the workbench beside the conversation, and the card reads **Open in a tab**
   with **Show tab**. **Return to conversation** in the tab brings it back to its
   card.

   ![The conversation on the left with the card reduced to Open in a tab and Show tab, and the globe in a workbench tab on the right with Ready and Return to conversation](plugin-asset://guide/captures/geo-in-tab.webp)
*The view is in one place at a time: here, in the tab.*

3. **Maximize it.** Choose the maximize control at the top right of the workbench, and
   collapse the left navigation if you want the whole window. Maximizing does not
   reload the view, so it keeps its camera and selection.

   ![The globe filling the window: Mount Rainier National Park with the ten labelled stations and the layer list](plugin-asset://guide/captures/geo-maximized.webp)
*Maximized, the view fills the window.*

4. **Let the agent look.** Ask about what is on screen. In a second conversation the
   agent had also drawn the park's boundary; asked "Look at it again and tell me what
   you see around CRBR", it took a picture of the view as shown in its tab, read what
   the view reports, and answered from both.

   ![The person says they opened the globe in a tab and asks the agent to look again and say what it sees around CRBR; the agent answers that CRBR sits on the park's northwest corner, describes a detached sliver of the park outline and the land around it, and checks the map against its table of stations inside and outside the park](plugin-asset://guide/captures/geo-look.webp)
*The agent looked at the view, then answered, and said what the picture could not settle.*

5. **Link it to a table.** Ask for a dashboard: "Make a dashboard with this globe
   next to a table of the stations, linked so that picking a station in either one
   shows it in the other." Pick a row and the globe turns to that station; click a dot
   and its row is highlighted. The dashboard says what is linked, here
   **selected_feature linked both ways**.

   ![A dashboard titled Rainier GNSS stations and the Cascadia subduction zone, with the Geo globe on the left showing station SNRS selected with its details, and a stations table on the right with the SNRS row highlighted; below the globe, selected_feature linked both ways](plugin-asset://guide/captures/geo-dashboard-linked.webp)
*The globe and the table share the selection. Built from that request in another conversation over the same example stations.*

6. **Come back later.** CLIO keeps the call that opened each view. After CLIO
   restarts, your app tabs come back in the same browser and each reopens when you
   show it. A card whose view has closed offers **Reopen**.

   ![After CLIO restarted, the second conversation beside its tab: the Geo view titled GNSS stations and Mount Rainier National Park, Ready, with the park boundary and the ten stations as before](plugin-asset://guide/captures/geo-reopened.webp)
*Reopened after a restart. Geo replays the view from a saved view id, not from a copy of the page.*

## Comment on a region of a view

Every app view has the Comments icon in its header, in a card, in a tab and in a
dashboard. Comments on an app work by region, as on a figure:

1. Choose the Comments icon. Drag a box over the part you mean, or click a point.

2. Write your comment and choose **Done**. Each mark is labelled S1, S2 and so on.

3. Choose **Add 1 mark to message** (or the number you made) and send the message.

The agent receives a picture of the view with your marks drawn on it, a close-up of
each mark, and what the view reported when you made it, such as the time step and
camera. Marks are of what the screen showed. They do not move the view back to that
state later. See [Comment on results](docs/comments.md) for comments on documents and
figures.

![The ParaView plugin's view of a pressure wave in a tab, with region S1 drawn over the isosurface shells and the comment editor reading: Are these inner rings separate pressure levels, or one surface folded back on itself?](plugin-asset://guide/captures/paraview-region-comment.webp)
*A region comment on a ParaView view, in the wave-study workspace.*

![The agent's answer beside the ParaView view: the warm side is a property of the temperature field, with the evidence from a temporary slice and the temperature range](plugin-asset://guide/captures/paraview-region-comment-answer.webp)
*The agent answered with evidence from the data, then restored the view.*

## In the conversation or in a tab

- Choose **Open in tab** on a card to give a view more room. Some apps ask for a tab
  themselves when they open; ParaView's does.
- Choose **Return to conversation** in the tab, or close the tab, to bring the view
  back to its card.
- The view is in one place at a time. Moving it reloads it, and CLIO hands it the same
  input and result again, so it shows the same thing.
- On a phone, an app tab sits between the conversation's header and the message box,
  so you can keep typing while it is open.

![A wave-study conversation beside a tab titled ParaView: wave.pvd, Ready, showing pressure isosurfaces coloured by temperature at time step 12 of 12, with a time slider](plugin-asset://guide/captures/paraview-in-tab.webp)
*The ParaView plugin opens its view in a tab. Its own controls, such as the time slider, work as in ParaView.*

## What stays live

An app keeps running while you can reach it:

- every app in a tab, until you close the tab,
- every app in a dashboard that is on screen,
- the newest app card in the conversation.

When a newer app opens in the conversation, the previous card's view closes and the
card reads **View closed**, with **Reopen**. Apps in tabs are never closed this way,
and nothing closes a view you have open because time passed. A card nobody has used
for an hour closes too.

## What the agent sees

- **What the view reports.** Many apps report what they show: the selected station,
  the time step, the camera. That report goes to the agent with the next message you
  send. Ask "what did I select?" or "what am I looking at?" and it answers from it.
- **The view itself.** The agent can look at an open view and see it as you do, with
  its `look_at_view` tool. The view must be on screen in a window that has the
  conversation open; if it is not, the agent's look waits until you show it.
- **The view's own tools.** An app can offer tools of its own, such as Geo's "fly to"
  or "select feature". The agent can call them, and the change appears in the view you
  have open. If the view is closed, the call returns at once and names the tool that
  opens it again.

Calls the agent makes to an app follow your [permissions](docs/permissions.md), like
any other tool.

## Reopen a view

- To bring back a closed view, choose **Reopen** on its card. CLIO calls the same tool
  with the same arguments and shows the new view.
- After CLIO restarts, your app tabs come back in the same browser. Each one reopens
  when you first show it.
- If a reopen fails, the card or tab gives the reason and the call it tried, with
  **Retry**.

Reopening calls the tool again, so it may ask for your permission. What the view shows
afterwards is what the service returns. ParaView brings back the scene with its
filters, colouring and time step; Geo brings back its layers and styles.

## Put an app in a dashboard

An app can sit in a [dashboard](docs/dashboards.md) beside charts and tables, linked to
them. Ask for it in plain words, as in step 5 above, or "Put the ParaView view and
charts of peak pressure and mean temperature over time in one dashboard, linked by
time step."

Linked parts share a value, such as a time step or a selection:

- Change the time in the view, and the charts follow.
- Pick a time on a chart, and the view moves to it.

The dashboard shows which values are linked, for example **time linked both ways**.
Only settled values cross between them, at most ten times a second, so a drag does not
flood the chart. An app in a dashboard stays in the dashboard; it does not open a tab.
See [App view](docs/widgets/mcp-app.md) for the component.

![A dashboard titled Pressure wave: isosurfaces and per-step summaries, with the ParaView view at step 12 of 12 beside charts of peak pressure and mean temperature per time step, labelled time linked both ways](plugin-asset://guide/captures/paraview-dashboard-time-linked.webp)
*A ParaView view and two charts linked by time, built from a plain request.*

![A dashboard titled MAGIC data assimilation (arXiv 2611.15421): paper and its datasets and models, with the PDF viewer showing page 1 of 49 on the left and a table of the models, datasets and forcings the paper uses on the right](plugin-asset://guide/captures/pdf-dashboard-table.webp)
*The PDF viewer connector in a dashboard, next to a table the agent built from the paper's methods sections.*

## Links and downloads

- A link in a view opens in CLIO's [browser](docs/browser.md) as a new page. Only
  `http` and `https` links open.
- A file a view offers for download is saved in the conversation's workspace folder
  and listed in **Artifacts**. A notice says where it went, with **Open**. An existing
  file is never overwritten: the new one gets a number. A file over 64 MB is refused.

## Apps with a live service

Some views, such as ParaView's, need a service running behind them. CLIO carries that
service through its own address, adds the service's key on the way, and never shows
the key to your browser or the agent. The view therefore works wherever your browser
is, including on another computer that reaches CLIO over your network.

If you reach CLIO by a network name rather than `localhost`, set
`gact.mcp_apps.sandbox_host` (or `CLIO_MCP_APP_SANDBOX_HOST`) to a name for app views,
such as `*.apps.example.lan` with wildcard DNS. Until it is set, an app card says
which setting it needs. See [Configuration](docs/configuration.md).

## Theme

Apps receive CLIO's colours, fonts and corner radii through the standard MCP Apps
theme variables, and again whenever you switch between light and dark. An app that
reads them matches CLIO. One that does not keeps its own look; the globe's map stays
light in both themes.

## Known limits

- **Burette in a narrow frame.** Burette's own viewer draws nothing when its frame is
  600 pixels wide or narrower, which includes a phone and a side panel at its usual
  width on a laptop screen. Maximize the side panel, or widen it. Burette's card in
  the conversation is small; open it in a tab.
- **Pictures of a view from another site.** When the agent captures a whole dashboard,
  a view that draws content from another site, such as the globe's map, appears as an
  empty area, and the agent says so. Its `look_at_view` sees the view itself.
- **A view that only the app understands.** CLIO shows what the app draws. When a
  view looks wrong, the cause is usually in the app. Reopening it is a quick check.
- **A conversation nobody is viewing.** CLIO stops a conversation's local services
  after it has been left alone for a while. Its views then read **View closed**;
  choose **Reopen**.

## Next steps

- [Connectors](docs/connectors.md): Find, enable and add the services that provide apps.
- [Open and edit documents](docs/working-with-files.md#view-3d-models): STEP, STL and other 3D models open in the file viewer.
- [Write a connector entry](docs/sdk/connectors.md): For app authors: the CLIO-specific keys and host features.
