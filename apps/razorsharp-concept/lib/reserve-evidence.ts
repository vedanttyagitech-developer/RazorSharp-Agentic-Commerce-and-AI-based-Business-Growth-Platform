import {commerce} from './reserve-api';
export type ReserveTerms={per_purchase_limit_minor:number;capacity_minor:number;allowed_skus?:string[]|null};
export async function downloadReserveEvidence(authorityId:string){
 const data=await commerce(`reserve/authorities/${authorityId}/proof`);
 download(data,`reserve-authorization-${authorityId}.json`);
}
export async function downloadReserveTrustKeys(){download(await commerce('reserve/verification-keys'),'reserve-public-keys.json')}
function download(data:unknown,name:string){
 const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
 const link=document.createElement('a');link.href=url;link.download=name;link.click();
 setTimeout(()=>URL.revokeObjectURL(url),1000);
}
