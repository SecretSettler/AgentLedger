// AgentLedger comment CSV synchronization for a hosted review adapter.
// Keep the token on the server. Only explicitly shared threads may flow back to a public page.
export const COMMENT_FIELDS = ["id","reply_to","report","version","module","kind","quote","prefix","suffix","body","author","role","status","created","updated","resolved_in"];
export function parseCommentCsv(text) {
  text=String(text).replace(/^\uFEFF/,"");
  const table=[];let row=[],field="",quoted=false;
  for(let i=0;i<text.length;i++) {
    const c=text[i];
    if(quoted) {if(c==='"') {if(text[i+1]==='"'){field+='"';i++;}else quoted=false;}else field+=c;continue;}
    if(c==='"') quoted=true;
    else if(c===","){row.push(field);field="";}
    else if(c==="\n"){row.push(field);table.push(row);row=[];field="";}
    else if(c!=="\r")field+=c;
  }
  if(quoted)throw new Error("Invalid quoted CSV");
  if(field || row.length){row.push(field);table.push(row);}
  if(!table.length)return [];
  const head=table.shift();
  if(!COMMENT_FIELDS.every(f=>head.includes(f)))throw new Error("Unexpected comment CSV schema");
  return table.filter(r=>r.some(Boolean)).map(r=>Object.fromEntries(head.map((f,i)=>[f,r[i]??""]))).filter(r=>r.id);
}
export function commentCsv(rows) {
  const cell=v=>{const s=String(v??"");return /[",\r\n]/.test(s)?'"'+s.replaceAll('"','""')+'"':s;};
  const sorted=rows.slice().sort((a,b)=>(a.created||"").localeCompare(b.created||"") || a.id.localeCompare(b.id));
  return [COMMENT_FIELDS.join(","),...sorted.map(r=>COMMENT_FIELDS.map(f=>cell(r[f])).join(","))].join("\n")+"\n";
}
function decode64(text){return new TextDecoder().decode(Uint8Array.from(atob(text.replace(/\s/g,"")),c=>c.charCodeAt(0)));}
function encode64(text){const bytes=new TextEncoder().encode(text);let s="";for(let i=0;i<bytes.length;i+=0x8000)s+=String.fromCharCode(...bytes.subarray(i,i+0x8000));return btoa(s);}
function validReport(id){return /^[a-z0-9]+(?:-[a-z0-9]+)*(?:\/[a-z0-9]+(?:-[a-z0-9]+)*)+$/.test(id);}
export class GitHubCommentStore {
  constructor({repo,branch="main",token,root="",fetcher=fetch}) {
    if(!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo))throw new Error("Invalid GitHub repository");
    if(!token)throw new Error("A server-side GitHub token is required");
    if(root && root.split("/").some(p=>!p || p==="." || p===".."))throw new Error("Invalid store root");
    Object.assign(this,{repo,branch,token,root,fetcher});
  }
  url(report,version) {
    if(!validReport(report) || !/^[1-9]\d*$/.test(String(version)))throw new Error("Invalid report or version");
    const file=[this.root,"projects",report,"v"+String(version).padStart(3,"0"),"comments.csv"].filter(Boolean).join("/");
    return "https://api.github.com/repos/"+this.repo+"/contents/"+file.split("/").map(encodeURIComponent).join("/");
  }
  headers(){return {Authorization:"Bearer "+this.token,Accept:"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28","User-Agent":"AgentLedger"};}
  async load(report,version) {
    const r=await this.fetcher(this.url(report,version)+"?ref="+encodeURIComponent(this.branch),{headers:this.headers(),signal:AbortSignal.timeout(12000)});
    if(r.status===404)throw new Error("Report version is missing in the canonical store");
    if(!r.ok)throw new Error("Canonical comment read failed ("+r.status+")");
    const j=await r.json();
    if(j.type && j.type!=="file")throw new Error("Expected a comment CSV file");
    return {rows:parseCommentCsv(decode64(j.content)),sha:j.sha};
  }
  async merge(report,version,incoming) {
    if(!incoming.length)return {rows:[],commit:null};
    for(const row of incoming)if(row.report!==report || String(row.version)!==String(version))throw new Error("Comment scope mismatch");
    const requested=new Set(incoming.map(r=>r.id));
    for(let attempt=0;attempt<3;attempt++) {
      const current=await this.load(report,version), merged=new Map(current.rows.map(r=>[r.id,r]));
      let changed=false;
      for(const row of incoming) {
        const old=merged.get(row.id);
        if(!old || row.updated>old.updated) {merged.set(row.id,row);changed=true;}
      }
      if(!changed)return {rows:current.rows.filter(r=>requested.has(r.id)),commit:null};
      const r=await this.fetcher(this.url(report,version),{method:"PUT",headers:{...this.headers(),"Content-Type":"application/json"},signal:AbortSignal.timeout(12000),body:JSON.stringify({
        message:"agentledger: sync "+incoming.length+" shared review update(s) on "+report+" v"+version,
        branch:this.branch,sha:current.sha,content:encode64(commentCsv([...merged.values()]))
      })});
      if((r.status===409 || r.status===422) && attempt<2)continue;
      if(!r.ok)throw new Error("Canonical comment write failed ("+r.status+")");
      const j=await r.json();
      return {rows:[...merged.values()].filter(r=>requested.has(r.id)),commit:j.commit?.sha||null};
    }
    throw new Error("Canonical comment synchronization conflicted");
  }
}
export function publicThreadRows(canonical,shared) {
  const ids=new Set(shared.map(r=>r.id));
  const roots=new Map(shared.filter(r=>!r.reply_to).map(r=>[r.id,r]));
  return canonical.filter(row=>{
    const known=shared.find(r=>r.id===row.id);
    if(ids.has(row.id))return row.report===known.report && row.version===known.version && row.reply_to===known.reply_to;
    const root=roots.get(row.reply_to);
    return !!root && row.role==="agent" && row.report===root.report && row.version===root.version;
  });
}
export async function syncHostedComments(db,github,report,{now=Date.now(),intervalMs=60000}={}) {
  const result=await db.prepare("SELECT id,row_json,updated_at,github_updated,github_checked FROM review_comments WHERE report = ?").bind(report).all();
  const groups=new Map();
  for(const record of result.results) {
    const row=JSON.parse(record.row_json), key=String(row.version);
    if(!groups.has(key))groups.set(key,[]);
    groups.get(key).push({...record,row});
  }
  const commits=[];
  for(const [version,records] of groups) {
    const pending=records.filter(r=>r.github_updated!==r.updated_at);
    const cutoff=new Date(now-intervalMs).toISOString(), at=new Date(now).toISOString();
    const candidates=pending.length?pending:records;
    let claimed=0;
    for(const record of candidates) {
      const r=await db.prepare("UPDATE review_comments SET github_checked = ? WHERE id = ? AND github_checked < ?").bind(at,record.id,cutoff).run();
      claimed+=r.meta?.changes||0;
    }
    if(!claimed)continue;
    try {
      const written=pending.length?await github.merge(report,version,pending.map(r=>r.row)):{rows:[],commit:null};
      if(written.commit)commits.push(written.commit);
      const canonical=await github.load(report,version);
      const publicRows=publicThreadRows(canonical.rows,records.map(r=>r.row));
      const byId=new Map(records.map(r=>[r.id,r]));
      const changes=[];
      for(const row of publicRows) {
        const old=byId.get(row.id);
        if(old && row.updated<old.updated_at)continue;
        if(old)changes.push(db.prepare("UPDATE review_comments SET row_json = ?,updated_at = ?,github_updated = ? WHERE id = ? AND updated_at = ?")
          .bind(JSON.stringify(row),row.updated,row.updated,row.id,old.updated_at));
        else changes.push(db.prepare("INSERT INTO review_comments (id,report,version,owner,row_json,created_at,updated_at,github_updated,github_checked) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING")
          .bind(row.id,report,+row.version,"agentledger",JSON.stringify(row),row.created,row.updated,row.updated,at));
      }
      if(changes.length)await db.batch(changes);
    } catch(error) { console.error("AgentLedger canonical sync:",error.message); }
  }
  const pending=await db.prepare("SELECT COUNT(*) AS n FROM review_comments WHERE report = ? AND github_updated != updated_at").bind(report).first();
  return {enabled:true,pending:pending.n,commits};
}
