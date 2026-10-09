const fs = require('fs');
const { searchPOI } = require('./index');
const tasks = [
  ['luoyangstation', '洛阳站', '洛阳'],
  ['hotel', '洛阳站附近酒店', '洛阳'],
  ['locheng', '洛邑古城', '洛阳'],
  ['yingtianmen', '应天门', '洛阳'],
  ['lijingmen', '丽景门', '洛阳'],
  ['crossStreet', '洛阳十字街', '洛阳'],
  ['mingtang', '明堂天堂', '洛阳'],
  ['jiuzhouchi', '九洲池', '洛阳'],
  ['longmenshiku', '龙门石窟', '洛阳'],
  ['guanlin', '关林', '洛阳'],
  ['museum', '洛阳博物馆', '洛阳'],
  ['baimasi', '白马寺', '洛阳'],
  ['dayunhe', '隋唐大运河文化博物馆', '洛阳'],
  ['wangcheng', '王城公园', '洛阳']
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const out = {};
  for (const [k, kw, city] of tasks) {
    try {
      const r = await searchPOI({ keywords: kw, city, page: 1, offset: 6 });
      out[k] = (r && r.pois ? r.pois : []).map((p) => ({ name: p.name, location: p.location, address: p.address, type: p.type }));
    } catch (e) {
      out[k] = 'ERR ' + e.message;
    }
    await sleep(700);
  }
  fs.writeFileSync('./probe_out.json', JSON.stringify(out, null, 1), 'utf8');
  console.log('done');
})();
