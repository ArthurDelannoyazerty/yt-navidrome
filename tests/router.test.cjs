"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const Router = require("../src/static/router.js");

const user = "admin";
const a = "11111111-1111-4111-8111-111111111111";
const b = "22222222-2222-4222-8222-222222222222";
const routes = [
  {user,tab:"music"},
  {user,tab:"music",trackId:a},
  {user,tab:"music",q:"Infected Mushroom",status:"AVAILABLE",page:3},
  {user,tab:"music",ignored:true},
  {user,tab:"approval"},
  {user,tab:"approval",trackId:a,status:"ALL"},
  {user,tab:"catalogue",kind:"recording",entityId:b},
  {user,tab:"catalogue",kind:"release-group",entityId:a,q:"IM25",localOnly:true},
  {user,tab:"album"},
  {user,tab:"album",familyId:a},
  {user,tab:"album",familyId:a,editionId:b,includeAssigned:true,q:"Business"},
  {user,tab:"graph",kind:"recording",entityId:a,focus:a,view:"catalogue"},
  {user,tab:"graph",view:"catalogue",q:"日本語",kinds:"recording,release-group",limit:400},
  {user,tab:"sources",sourceId:a},
  {user,tab:"help",topic:"approval"},
  ...["language","activity","maintenance","help","sources","graph","catalogue"].map(tab=>({user,tab})),
];

test("each feature and selected item has a canonical, round-tripping URL",()=>{
  for(const route of routes){
    const url=Router.build(route);
    assert.ok(url && url.startsWith("/libraries/admin/"),JSON.stringify(route));
    const parts=new URL(url,"https://example.test");
    const parsed=Router.parse(parts.pathname,parts.search);
    assert.ok(parsed, url);
    assert.equal(parsed.user, user, url);
    assert.equal(parsed.tab, route.tab, url);
    for(const field of ["trackId","familyId","editionId","entityId","sourceId","kind","topic",
                         "q","status","page","view","focus","kinds","limit"]){
      if(route[field]!==undefined)assert.equal(parsed[field],route[field],url+" "+field);
    }
    if(route.ignored)assert.equal(parsed.ignored,true);
    if(route.includeAssigned)assert.equal(parsed.includeAssigned,true);
    assert.equal(Router.build(parsed),url);
  }
});

test("malformed, unrelated or misleading URLs never become application pages",()=>{
  for(const path of [
    "/api/tracks", "/static/app.js", "/libraries", "/libraries//music",
    "/libraries/admin/not-a-page", "/libraries/admin/approval/one/two",
    "/libraries/admin/albums/one/release/two", "/libraries/admin/catalogue/bad/x",
    "/libraries/admin/music/one/two", "/libraries/admin/music/%2Fapi",
    "/libraries/admin/sources/foo/bar", "/libraries/admin/graph/other/x",
  ])assert.equal(Router.parse(path),null,path);
  assert.equal(Router.parse("/").legacy,true);
  assert.equal(Router.build({tab:"music"}),null);
});

test("page URLs encode user and text filters, leaving unsaved decisions out of links",()=>{
  const route={user:"my_library",tab:"album",familyId:a,editionId:b,
    q:"It’s Behind Me Now & Friends",includeAssigned:true,
    manual:new Map([["local","official"]]),preview_token:"secret"};
  const url=Router.build(route);
  assert.ok(url.includes("my_library"));
  assert.ok(url.includes("q="));
  assert.ok(!url.includes("secret")&&!url.includes("preview_token")&&!url.includes("local"));
  assert.equal(Router.parse(new URL(url,"http://x").pathname,new URL(url,"http://x").search).q,
    route.q);
});

test("history writer deduplicates current locations and distinguishes push/replace",()=>{
  const oldLocation=globalThis.location,oldHistory=globalThis.history;
  const changes=[];
  try {
    globalThis.location={pathname:"/",search:""};
    globalThis.history={
      pushState(_state,_unused,url){changes.push(["push",url]);const parts=new URL(url,"https://test");
        globalThis.location={pathname:parts.pathname,search:parts.search};},
      replaceState(_state,_unused,url){changes.push(["replace",url]);const parts=new URL(url,"https://test");
        globalThis.location={pathname:parts.pathname,search:parts.search};},
    };
    assert.equal(Router.write({user,tab:"music"},{replace:true}),true);
    assert.equal(Router.write({user,tab:"music"}),false);
    assert.equal(Router.write({user,tab:"album",familyId:a}),true);
    assert.deepEqual(changes.map(x=>x[0]),["replace","push"]);
    assert.equal(Router.read().familyId,a);
  } finally {
    if(oldLocation===undefined)delete globalThis.location;else globalThis.location=oldLocation;
    if(oldHistory===undefined)delete globalThis.history;else globalThis.history=oldHistory;
  }
});
