export type CheckoutChoice='manual'|'reserve'|'clarify';
export function checkoutChoice(text:string):CheckoutChoice{
 // STT can spell Hindi loanwords with or without nukta; both are the same choice.
 const s=text.trim().toLowerCase().normalize('NFD').replace(/\u093c/g,'');
 if(/\b(what|how|explain|difference|kya|kaise|not|never|cancel|nahi|mat)\b|don['’]?t|क्या|नहीं|मत/.test(s))return 'clarify';
 const reserve=/\b(reserve|uap|ai)\b|यूपीआई|रिजर्व/.test(s);
 const manual=/\b(razorpay|manual|manually|netbanking|card|cards)\b|रेजर\s*पे|रेजोर\s*पे/.test(s);
 return reserve===manual?'clarify':reserve?'reserve':'manual';
}
export function canChoosePayment(count:number,stage:string,handoff:string){return count>0&&!['checking','confirmed'].includes(stage)&&['choose','failed'].includes(handoff)}
