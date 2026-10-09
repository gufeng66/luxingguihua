const fs = require('fs');
const api = require('./index');
const routeFn = {
  walking: api.walkingRoute,
  driving: api.drivingRoute,
  riding: api.ridingRoute,
  transfer: api.transitRoute
};
const legs = [
  ['d1_a_station_to_hotel', 'walking', '112.436284,34.685924', '112.436074,34.684473', ''],
  ['d1_b_hotel_to_lijingmen', 'transfer', '112.436074,34.684473', '112.471252,34.680899', '洛阳'],
  ['d1_c_lijingmen_to_yingtianmen', 'transfer', '112.471252,34.680899', '112.460886,34.676011', '洛阳'],
  ['d1_d_yingtianmen_to_mingtang', 'walking', '112.460886,34.676011', '112.459829,34.680653', ''],
  ['d1_e_mingtang_to_cross', 'transfer', '112.459829,34.680653', '112.478954,34.682918', '洛阳'],
  ['d1_f_cross_to_hotel', 'transfer', '112.478954,34.682918', '112.436074,34.684473', '洛阳'],
  ['d2_a_hotel_to_museum', 'transfer', '112.436074,34.684473', '112.451541,34.643323', '洛阳'],
  ['d2_b_museum_to_longmen', 'transfer', '112.451541,34.643323', '112.477482,34.558727', '洛阳'],
  ['d2_c_longmen_to_guanlin', 'transfer', '112.477482,34.558727', '112.483366,34.607419', '洛阳'],
  ['d2_d_guanlin_to_hotel', 'transfer', '112.483366,34.607419', '112.436074,34.684473', '洛阳'],
  ['d3_a_hotel_to_baimasi', 'transfer', '112.436074,34.684473', '112.605311,34.721828', '洛阳'],
  ['d3_b_baimasi_to_wangcheng', 'transfer', '112.605311,34.721828', '112.421575,34.666822', '洛阳'],
  ['d3_c_wangcheng_to_hotel', 'transfer', '112.421575,34.666822', '112.436074,34.684473', '洛阳'],
  ['d3_d_hotel_to_station', 'walking', '112.436074,34.684473', '112.436284,34.685924', '']
];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const out = {};
  for (const [k, type, origin, dest, city] of legs) {
    try {
      const p = { origin, destination: dest, type };
      if (city) p.city = city;
      const r = await routeFn[type](p);
      out[k] = { type, origin, dest, raw: r };
    } catch (e) {
      out[k] = { type, origin, dest, err: e.message };
    }
    await sleep(700);
  }
  fs.writeFileSync('./route_out.json', JSON.stringify(out, null, 1), 'utf8');
  console.log('done');
})();
