import {ArrowRight} from 'lucide-react';
export function CampaignArtwork({title,body,onSelect}:{title:string;body:string;onSelect?:()=>void}){
 return <div className="shared-campaign-art"><div><span>GREEN BASKET · MERCHANT PROMOTION</span><h2>{title||'Your campaign headline'}</h2><p>{body||'Your campaign message appears here.'}</p>{onSelect?<button onClick={onSelect}>Explore breakfast picks <ArrowRight size={16}/></button>:<span className="campaign-preview-cta">Explore breakfast picks <ArrowRight size={16}/></span>}</div><div className="shared-campaign-image"><img src="/products/BRIT-BAKE-001.webp" alt="Britannia brown bread"/><small>BREAKFAST, YOUR WAY</small></div></div>
}
