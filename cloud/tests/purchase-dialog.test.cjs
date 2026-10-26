const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync(require('node:path').join(__dirname,'../swgoh_cloud/panel/app.js'),'utf8');
const code=source.slice(source.indexOf('let activePurchase='),source.indexOf('const purchaseLabels='));
function fixture(){
 const dialog={open:true,close(){this.open=false;}},callbacks=new Map();let seq=0,refreshed=0;
 const context=vm.createContext({$:()=>dialog,state:{purchases:{commands:[]}},generation:1,setTimeout(fn,ms){assert.equal(ms,1400);callbacks.set(++seq,fn);return seq;},clearTimeout(id){callbacks.delete(id);},refresh(){refreshed++;}});
 vm.runInContext(code,context);vm.runInContext("activePurchase='one';closeOnSuccess='one';",context);
 return {context,dialog,callbacks,get refreshed(){return refreshed;},view(state='succeeded',id='one',kind){const c={id,state,expected:{kind}};context.state.purchases.commands=[c];context.c=c;vm.runInContext('closeSuccessfulPurchase(c)',context);}};
}
test('success is shown before one delayed close and refresh',()=>{const f=fixture();f.view();f.view();assert.equal(f.callbacks.size,1);assert.equal(f.dialog.open,true);[...f.callbacks.values()][0]();assert.equal(f.dialog.open,false);assert.equal(f.refreshed,1);});
test('pending, errors and uncertain outcomes are never automatically closed',()=>{for(const state of ['prepared','sending','failed','uncertain']){const f=fixture();f.view(state);assert.equal(f.callbacks.size,0);assert.equal(f.dialog.open,true);}});
test('old timer cannot close another purchase or another login',()=>{for(const change of ["activePurchase='two'","generation++"]){const f=fixture();f.view();vm.runInContext(change,f.context);[...f.callbacks.values()][0]();assert.equal(f.dialog.open,true);}});
test('opening an already successful history record is not armed',()=>{const f=fixture();vm.runInContext('closeOnSuccess=null',f.context);f.view();assert.equal(f.callbacks.size,0);});

test('successful inbox Claim also closes after the receipt',()=>{const f=fixture();f.view('succeeded','one','inbox');assert.equal(f.callbacks.size,1);[...f.callbacks.values()][0]();assert.equal(f.dialog.open,false);});
