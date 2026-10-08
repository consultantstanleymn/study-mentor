import sys,re,glob
for f in sorted(glob.glob(f"evals/out/{sys.argv[1]}-*.md")):
    t=open(f).read(); ms=[l for l in t.splitlines() if l.startswith("**MENTOR**")]
    w=[len(l.split())-1 for l in ms]
    print(f.split('/')[-1], "turns",len(ms),"avgw",sum(w)//max(1,len(w)),"max",max(w),"logs",len(re.findall(r"log:",t)),"miss",len(re.findall(r"=miss",t)),"covered",len(re.findall(r"covered:",t)),"todo",len(re.findall(r"TODO:",t)),re.findall(r"day_done:\w+",t),"ERR" if "ERROR" in t else "")
