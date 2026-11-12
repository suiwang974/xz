import os,re,importlib,sys
root=os.path.dirname(os.path.dirname(os.path.abspath(__file__)));sys.path.insert(0,root)
spec=open(os.path.join(root,'SniffDL.spec'),encoding='utf-8').read()
ok=bad=0
def chk(n,c):
    global ok,bad
    print(('PASS ' if c else 'FAIL ')+n); ok+=c; bad+=(not c)
for f in sorted(os.listdir(os.path.join(root,'core'))):
    if f.endswith('.py') and f!='__init__.py':
        m='core.'+f[:-3]
        chk(m+' 在spec', "'%s'"%m in spec)
        try: importlib.import_module(m); chk(m+' 可导入',True)
        except Exception as e: chk(m+' 可导入(沙箱缺libtk则跳过) %s'%e,'libtk' in str(e))
for m in ['sdl_speed','sdl_speed.engine','core.speed']:
    chk(m+' 在spec',"'%s'"%m in spec)
print('%d/%d'%(ok,ok+bad)); sys.exit(bad>0)
