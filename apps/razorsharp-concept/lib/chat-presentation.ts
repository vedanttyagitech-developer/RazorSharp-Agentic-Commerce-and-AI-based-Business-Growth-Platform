/** Presentation only: retain the full reply for voice and saved conversation history. */
export function visibleAgentReply(reply: string | null | undefined): string {
  if (!reply) return '';
  const acknowledgement = /^(?:Added \d+ × .+?\. \d+ now in your cart\.|Removed \d+ × .+?\. (?:\d+ now in your cart\.|Removed from your cart\.)|.+? के \d+ पैक जोड़े। कार्ट में अब \d+ हैं।|.+? ke \d+ packs add hue\. Cart mein ab \d+ hain\.)\s*/u;
  if (!acknowledgement.test(reply)) return reply;
  return reply.replace(acknowledgement, '')
    .replace(/^Would you like to consider .+? alongside it\? Optional—nothing else was added\.\s*/u, '')
    .replace(/^साथ में .+? देखना चाहेंगे\? यह सिर्फ सुझाव है; और कुछ नहीं जोड़ा।\s*/u, '')
    .replace(/^Saath mein .+? dekhna chahenge\? Sirf suggestion hai; aur kuch add nahi hua\.\s*/u, '')
    .replace(/^Anything else, or shall we review your bill\?\s*/u, '').trim();
}
