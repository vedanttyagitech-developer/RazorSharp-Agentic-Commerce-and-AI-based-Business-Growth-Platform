// `sku` is the backend catalogue identity and the only field that may be sent to the API.
// It used to be derived from the image filename (`skuOf`), which made product identity
// depend on an asset path: renaming a picture would have renamed the thing being bought.
// `price` and `stock` here are shelf rendering only -- the backend prices every bill.
export type Product = {id:string;sku:string;name:string;unit:string;price:number;category:string;symbol:string;color:string;stock:number;image:string};
export const products:Product[]=[
 {sku:'AMUL-DAIRY-001',id:'milk',name:'Amul Taaza milk',unit:'500 ml · Toned milk',price:2800,category:'Dairy',symbol:'🥛',color:'#eaf1fc',stock:42,image:'/products/AMUL-DAIRY-001.webp'},
 {sku:'BANA-PROD-006',id:'avocado',name:'Fresh bananas',unit:'6 pieces · Farm fresh',price:5400,category:'Produce',symbol:'🍌',color:'#eef3e9',stock:18,image:'/products/BANA-PROD-006.webp'},
 {sku:'BRIT-BAKE-001',id:'bread',name:'Britannia brown bread',unit:'400 g · Bakery',price:5000,category:'Bakery',symbol:'🍞',color:'#f7efe7',stock:12,image:'/products/BRIT-BAKE-001.webp'},
 {sku:'NAGP-PROD-027',id:'oranges',name:'Nagpur oranges',unit:'1 kg · Farm fresh',price:9500,category:'Produce',symbol:'🍊',color:'#fff0df',stock:35,image:'/products/NAGP-PROD-027.webp'},
 {sku:'AMUL-DAIRY-003',id:'eggs',name:'Amul Masti dahi',unit:'400 g · Dairy',price:4500,category:'Dairy',symbol:'🥣',color:'#f6f0e9',stock:7,image:'/products/AMUL-DAIRY-003.webp'},
 {sku:'NESC-BEVG-002',id:'coffee',name:'Nescafé Classic',unit:'50 g · Instant coffee',price:18500,category:'Pantry',symbol:'☕',color:'#eee9e7',stock:23,image:'/products/NESC-BEVG-002.webp'},
 {sku:'SPIN-PROD-016',id:'broccoli',name:'Fresh spinach',unit:'250 g · Farm fresh',price:2200,category:'Produce',symbol:'🥬',color:'#eaf3e9',stock:26,image:'/products/SPIN-PROD-016.webp'},
 {sku:'AMUL-DAIRY-004',id:'honey',name:'Amul Malai paneer',unit:'200 g · Dairy',price:9500,category:'Dairy',symbol:'🧀',color:'#fff3d9',stock:14,image:'/products/AMUL-DAIRY-004.webp'},
];
export const money=(n:number)=>new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:n%100?2:0}).format(n/100);
export type DemoAction={id:string;title:string;kind:string;before:string;after:string;status:'Awaiting approval'|'Applied'|'Rejected'|'Stale';by:string;time:string};
export const initialActions:DemoAction[]=[
 {id:'MA-1042',title:'Restock Amul Masti dahi',kind:'Inventory',before:'7 units',after:'40 units',status:'Awaiting approval',by:'Operations Assistant',time:'8 min ago'},
 {id:'MA-1041',title:'Weekend breakfast offer',kind:'Merchant Policy',before:'No active offer',after:'10% off · maximum ₹50',status:'Awaiting approval',by:'Pricing & Promotions',time:'21 min ago'},
 {id:'MA-1040',title:'List fresh spinach',kind:'Catalogue',before:'Hidden',after:'Listed',status:'Applied',by:'You',time:'1 hour ago'},
];
export const sampleOrders=[{id:'RS-0909-A4F2',customer:'Ananya S.',items:'Bananas, brown bread + 2',amount:38900,status:'Confirmed',time:'2 min ago'}, {id:'RS-0909-B7K9',customer:'Rahul M.',items:'Milk, dahi + 1',amount:28600,status:'Checking payment',time:'8 min ago'}, {id:'RS-0909-C2L5',customer:'Priya K.',items:'Coffee, paneer',amount:43800,status:'Delivered',time:'28 min ago'}, {id:'RS-0908-D8M3',customer:'Arjun R.',items:'Milk, oranges',amount:11500,status:'Partially refunded',time:'Yesterday'}];
export const capabilities=[['Shopping & search','Catalogue APIs','Available backend'],['Cart & quantity','Cart APIs','Available backend'],['Checkout & consent','Atomic approval + admission','Available backend'],['Payment & reconciliation','Provider evidence + worker','Available backend'],['Merchant actions','Six supported action kinds','Available backend'],['Merchant Policy','Versioned publication','Available backend'],['Customer support','Case intake + merchant queue','Available backend'],['Transaction proof','Proof + audit verification APIs','Available backend'],['Voice conversation','Gateway exists; browser integration required','Integration pending'],['Merchant copilot','Specialist harness and grounded tools','Planned experience'],['Item refund plans','Durable Resolution Plan confirmation','Planned experience'],['Reserve Pay','Saved product permissions, Kernel admission and durable execution','Server-side provider simulation'],['Growth analytics','Aggregate reads + attribution','Planned experience'],['WhatsApp','Adapter reserved','Future / disabled']];
