export type CheckoutChoice='manual'|'reserve'|'clarify';
export function checkoutChoice(text:string):CheckoutChoice{
 // STT can spell Hindi loanwords with or without nukta; both are the same choice.
 const s=text.trim().toLowerCase().normalize('NFD').replace(/\u093c/g,'');
 // Mentioning a method in a question, condition or refusal is not a payment choice.
 if(/[?？]|\b(what|how|why|when|where|which|explain|difference|kya|kaise|kyu|not|never|cancel|stop|wait|nahi|nahin|mat|dont|if|unless|whether|balance|limit|safe|enough)\b|don['’]?t|क्या|क्यों|कैसे|कौन|कब|नहीं|नही|मत|रुको|अगर|यदि|बैलेंस|सीमा/.test(s))return 'clarify';
 if(/^(can|could|should|would|is|does|will)\b/.test(s))return 'clarify';
 const reserve=/\b(reserve|uap|ai)\b|यू\s*ए\s*पी|एआई|रिजर्व/.test(s);
 // UPI is a manual Razorpay method; it must never be confused with UAP authority.
 const manual=/\b(razorpay|manual|manually|netbanking|card|cards|upi)\b|यूपीआई|यू\s*पी\s*आई|रेजर\s*पे|रेजोर\s*पे/.test(s);
 const bare=/^(please\s+)?(reserve(\s+pay)?|uap|ai|razorpay|manual(ly)?|netbanking|cards?|upi|रिजर्व\s*पे|यू\s*ए\s*पी|एआई|यूपीआई|यू\s*पी\s*आई|रेजर\s*पे|रेजोर\s*पे)(\s+please)?[.!।]*$/.test(s);
 const action=/\b(pay|use|choose|select|proceed|continue|karo|kardo|kijiye|kariye|karna)\b|भुगतान\s*(कर|करो|करें)|पेमेंट\s*(कर|करो|करें)|इस्तेमाल\s*(कर|करो|करें)|से\s*(करो|करें)/.test(s.replace(/\breserve\s+pay\b/g,''));
 if(!bare&&!action)return 'clarify';
 return reserve===manual?'clarify':reserve?'reserve':'manual';
}
export function canChoosePayment(count:number,stage:string,handoff:string){return count>0&&!['checking','confirmed'].includes(stage)&&['choose','failed'].includes(handoff)}
