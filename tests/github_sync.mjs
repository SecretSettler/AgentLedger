import {test} from "node:test";
import assert from "node:assert/strict";
import {GitHubCommentStore,parseCommentCsv,commentCsv,publicThreadRows} from "../agentledger/assets/github-sync.mjs";
const row=(id,extra={})=>({id,reply_to:"",report:"demo/report",version:"1",module:"results",kind:"question",body:"Check this",author:"Reader",role:"human",status:"open",created:"2026-01-01T00:00:00Z",updated:"2026-01-01T00:00:00Z",...extra});
function fakeGitHub(initial,{conflict=false}={}) {
 let rows=initial, sha="sha0",puts=0, conflictDone=false;
 return {get rows(){return rows;},get puts(){return puts;},fetcher:async(url,options={})=>{
  assert.match(url,/^https:\/\/api\.github\.com\/repos\/owner\/ledger\/contents\/projects\/demo\/report\/v001\/comments\.csv/);
  assert.equal(options.headers.Authorization,"Bearer test-token");
  if(options.method!=="PUT")return Response.json({type:"file",sha,content:Buffer.from(commentCsv(rows)).toString("base64")});
  const body=JSON.parse(options.body);
  if(conflict && !conflictDone){conflictDone=true;rows.push(row("private-racer"));sha="race";return new Response("",{status:409});}
  assert.equal(body.sha,sha);
  rows=parseCommentCsv(Buffer.from(body.content,"base64").toString("utf8"));sha="new"+(++puts);
  return Response.json({commit:{sha}});
 }};
}
test("CSV retains quotes, commas, Unicode and multiline text",()=>{
 const original=row("c_unicode",{body:'中文, "quote"\nnext line'});
 assert.equal(parseCommentCsv(commentCsv([original]))[0].body,original.body);
});
test("merges shared rows without deleting or exposing private threads",async()=>{
 const github=fakeGitHub([row("private")]);
 const store=new GitHubCommentStore({repo:"owner/ledger",token:"test-token",fetcher:github.fetcher});
 const result=await store.merge("demo/report",1,[row("public")]);
 assert.equal(github.rows.length,2);assert.deepEqual(result.rows.map(r=>r.id),["public"]);assert.equal(github.puts,1);
});
test("idempotent saves do not create repeated commits",async()=>{
 const github=fakeGitHub([row("public")]);
 await new GitHubCommentStore({repo:"owner/ledger",token:"test-token",fetcher:github.fetcher}).merge("demo/report",1,[row("public")]);
 assert.equal(github.puts,0);
});
test("retries a GitHub SHA conflict and preserves the concurrent writer",async()=>{
 const github=fakeGitHub([row("private")],{conflict:true});
 await new GitHubCommentStore({repo:"owner/ledger",token:"test-token",fetcher:github.fetcher}).merge("demo/report",1,[row("public")]);
 assert.deepEqual(new Set(github.rows.map(r=>r.id)),new Set(["private","private-racer","public"]));
});
test("public reads only contain known rows and agent answers to shared roots",()=>{
 const publicRoot=row("public");
 const rows=[publicRoot,row("private"),row("answer",{reply_to:"public",role:"agent"}),row("private-note",{reply_to:"public"}),row("other-report",{reply_to:"public",role:"agent",report:"other/report"})];
 assert.deepEqual(publicThreadRows(rows,[publicRoot]).map(r=>r.id),["public","answer"]);
});
test("newer canonical edits win over stale browser edits",async()=>{
 const current=row("public",{updated:"2026-02-01T00:00:00Z",status:"resolved"});
 const github=fakeGitHub([current]);
 const result=await new GitHubCommentStore({repo:"owner/ledger",token:"test-token",fetcher:github.fetcher}).merge("demo/report",1,[row("public")]);
 assert.equal(result.rows[0].status,"resolved");assert.equal(github.puts,0);
});
test("rejects traversal and cross-report writes before sending a request",async()=>{
 const store=new GitHubCommentStore({repo:"owner/ledger",token:"test-token",fetcher:()=>{throw new Error("must not fetch")}});
 assert.throws(()=>store.url("../private",1),/Invalid report/);
 await assert.rejects(store.merge("demo/report",1,[row("bad",{report:"private/report"})]),/scope/);
});

test("default fetch keeps the host runtime receiver",async()=>{
 const saved=globalThis.fetch, github=fakeGitHub([row("public")]);
 globalThis.fetch=function(...args){assert.equal(this,globalThis);return github.fetcher(...args);};
 try {
  const store=new GitHubCommentStore({repo:"owner/ledger",token:"test-token"});
  await store.merge("demo/report",1,[row("public")]);
 } finally {globalThis.fetch=saved;}
});
