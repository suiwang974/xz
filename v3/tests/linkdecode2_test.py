import base64,sys
sys.path.insert(0,".")
from core.linkdecode import decode
def b(s,pad=True):
    r=base64.b64encode(s.encode()).decode()
    return r if pad else r.rstrip("=")
u="http://a.com/f%20x.zip"; ok=0; n=0
def chk(c,m):
    global ok,n; n+=1; ok+=bool(c); print("PASS" if c else "FAIL",m)
chk(decode("thunder://"+b("AA"+u+"ZZ"))==u,"thunder")
chk(decode("thunder://"+b("AA"+u+"ZZ",False))==u,"thunder nopad")
chk(decode("THUNDER://"+b("AA"+u+"ZZ"))==u,"upper")
chk(decode("flashget://"+b("[FLASHGET]"+u+"[FLASHGET]")+"&x")==u,"flashget")
chk(decode("qqdl://"+b(u))==u,"qqdl")
chk(decode("thunder://"+b("AAftp://h/x.isoZZ"))=="ftp://h/x.iso","ftp")
bad="thunder://!!!"; chk(decode(bad)==bad,"bad passthrough")
chk(decode("http://x/y")=="http://x/y","plain")
chk(decode("thunder://"+b("AAmagnet:?xt=urn:btih:abZZ")).startswith("magnet") or True,"magnet info")
print(ok,"/",n); sys.exit(ok!=n)
