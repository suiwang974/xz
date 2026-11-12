import re,sys
t=open('docs/wintest用例清单.md',encoding='utf8').read()
n=len(re.findall(r'^\d+\. ',t,re.M)); print('cases',n); sys.exit(0 if n>=4 else 1)
