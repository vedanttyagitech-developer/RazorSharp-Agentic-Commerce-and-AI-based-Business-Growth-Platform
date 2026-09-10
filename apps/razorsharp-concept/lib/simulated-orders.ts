// Browser-only demonstration records. Never send these IDs to financial APIs.
export type DemoOrder={id:string;title:string;method:string;delivery:string;lines:{name:string;image:string;paid:number}[];fee:number;case?:{line:number;status:'OPEN'|'REFUND_PENDING'|'REFUNDED'|'DECLINED';approvedMinor?:number};refunded:number};
export const DEMO_ORDERS:DemoOrder[]=[
 {id:'DEMO-PAID-101',title:'Your morning essentials',method:'Razorpay · simulated',delivery:'Delivered',lines:[{name:'Amul Taaza milk',image:'/products/AMUL-DAIRY-001.webp',paid:2800},{name:'Britannia brown bread',image:'/products/BRIT-BAKE-001.webp',paid:5000}],fee:2500,refunded:0},
 {id:'DEMO-PAID-102',title:'A little coffee break',method:'Razorpay · simulated',delivery:'Delivered',lines:[{name:'Nescafé Classic',image:'/products/NESC-BEVG-002.webp',paid:18500},{name:'Fresh bananas',image:'/products/BANA-PROD-006.webp',paid:5400}],fee:0,refunded:0},
 {id:'DEMO-PAID-103',title:'Dinner, sorted',method:'Razorpay · simulated',delivery:'Delivered · item issue reported',lines:[{name:'Amul Malai paneer',image:'/products/AMUL-DAIRY-004.webp',paid:9500},{name:'Fresh spinach',image:'/products/SPIN-PROD-016.webp',paid:2200}],fee:2500,refunded:0,case:{line:0,status:'OPEN'}},
];
export type DemoChange={kind:'escalate';line:number}|{kind:'refund';amountMinor?:number}|{kind:'decline'}|{kind:'settle'};
export function changeDemoOrder(order:DemoOrder,action:DemoChange):DemoOrder{
 if(action.kind==='escalate'){if(order.case||!Number.isInteger(action.line)||!order.lines[action.line])return order;return {...order,case:{line:action.line,status:'OPEN'}};}
 if(action.kind==='settle'){if(order.case?.status!=='REFUND_PENDING')return order;return {...order,refunded:order.case.approvedMinor??0,case:{...order.case,status:'REFUNDED'}};}
 if(order.case?.status!=='OPEN')return order;
 if(action.kind==='decline')return {...order,case:{...order.case,status:'DECLINED'}};
 const paid=order.lines[order.case.line].paid;const amount=action.amountMinor??paid;
 if(!Number.isSafeInteger(amount)||amount<=0||amount>paid)return order;
 return {...order,case:{...order.case,approvedMinor:amount,status:'REFUND_PENDING'}};
}
