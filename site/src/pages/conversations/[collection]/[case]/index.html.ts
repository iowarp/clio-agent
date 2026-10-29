import type { APIRoute, GetStaticPaths } from 'astro';
import { conversationCollections, readPublishedConversation } from '../../../../lib/published-conversations';

export const getStaticPaths: GetStaticPaths = () =>
  conversationCollections.flatMap((collection) =>
    collection.conversations.map((asset) => ({
      params: { collection: collection.id, case: asset.slug },
      props: { collection, asset },
    })),
  );

/** Serve the exact self-contained native viewer as a static HTML page. */
export const GET: APIRoute = async ({ props }) => {
  const content = await readPublishedConversation(props.collection, props.asset);
  return new Response(content as Uint8Array<ArrayBuffer>, {
    headers: { 'Content-Type': 'text/html; charset=utf-8' },
  });
};
