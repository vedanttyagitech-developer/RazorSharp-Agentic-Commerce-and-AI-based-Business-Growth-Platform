/** Surface navigation only. Never cancels a payment or a placed order. */
export function shopNavigation(text:string):'basket'|'close-review'|null {
 const s=text.toLowerCase().trim().replace(/[.!?।]+$/,'').replace(/^(please|can you|could you)\s+/,'').replace(/\s+please$/,'');
 if(/^(show|open)( me)?( my| the)? (basket|cart)$|^(mera |meri )?(cart|basket) (dikhao|kholo)$|^(मेरा |मेरी )?(कार्ट|बास्केट) (दिखाओ|खोलो)$/.test(s))return 'basket';
 if(/^(cancel|close|exit)( the| my)? (checkout|order review|review|checkout review)$|^(checkout|review|order review) (cancel|band)( karo| kar do)?$|^(चेकआउट|रिव्यू) (बंद|कैंसल) करो$/.test(s))return 'close-review';
 return null;
}
