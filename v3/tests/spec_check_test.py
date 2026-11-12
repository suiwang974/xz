import re,glob,os,sys
spec=open('SniffDL.spec',encoding='utf-8').read()
miss=[]
for f in glob.glob('core/*.py'):
    m=os.path.basename(f)[:-3]
    if m=='__init__':continue
    if f"'core.{m}'" not in spec: miss.append('core.'+m)
for m in ['sdl_speed','sdl_speed.engine','rpc_server','native_host','netdisk_rules','component_updates','pet_bridge']:
    if f"'{m}'" not in spec: miss.append(m)
for f in glob.glob('sdl_speed/*.py'):
    m=os.path.basename(f)[:-3]
    if m!='__init__' and f"'sdl_speed.{m}'" not in spec: miss.append('sdl_speed.'+m)
print('MISSING',miss); print('PASS' if not miss else 'FAIL'); sys.exit(bool(miss))
