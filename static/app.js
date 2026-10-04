'use strict';
const $ = id => document.getElementById(id);
const map = L.map('map').setView([33.35,130.1],11);
L.control.scale({imperial:false}).addTo(map);
const tiles = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'});
$('tiles').onchange = () => $('tiles').checked ? tiles.addTo(map) : map.removeLayer(tiles);
tiles.on('tileerror',()=>{$('message').textContent='背景地図を取得できません。KMLと車両表示は継続します。';});
const markers = new Map();
let courseLayer, course, selected, state;
const rows = new Map(), visuals = new Map();
let session, generation = 0, detailsVersion;
function textNode(text){const el=document.createElement('span');el.textContent=text;return el;}
async function api(url, options){const r=await fetch(url,options);const data=await r.json();if(!r.ok)throw Error(data.error||r.statusText);return data;}
function select(key,pan=false){selected=key;render();if(pan){map.setView(markers.get(key).getLatLng(),15);markers.get(key).openPopup();}}
function render(){
  if(!state)return;
  $('count').textContent=state.vehicles.length;
  $('receiver').textContent=state.input_status;
  $('stats').textContent=`受信 ${state.total} 件 / 不正 ${state.invalid} 件`;
  if(session!==state.log){
    for(const marker of markers.values())map.removeLayer(marker);
    markers.clear();rows.clear();visuals.clear();selected=undefined;detailsVersion=undefined;
    $('vehicles').replaceChildren();$('details').textContent='地図上の車両番号をクリック';session=state.log;
  }
  if(state.vehicles.length && !rows.size)$('vehicles').replaceChildren();
  for(const v of state.vehicles){
    const stale=v.age_s>10;
    const appearance=`${v.id}|${v.status}|${v.source}|${stale}`;
    let icon;
    if(visuals.get(v.key)!==appearance){
    const badge=textNode(v.id);badge.className='badge'+(['SOS','CRASH'].includes(v.status)?' sos':v.source==='demo'?' demo':'')+(stale?' stale':'');
    icon=L.divIcon({html:badge,iconSize:[60,28],iconAnchor:[30,14]});
    visuals.set(v.key,appearance);
    }
    if(!markers.has(v.key)){
      const marker=L.marker([v.lat,v.lon],{icon}).addTo(map);
      marker.on('click',()=>select(v.key));
      marker.on('popupopen',()=>render());
      markers.set(v.key,marker);
    }
    const marker=markers.get(v.key);
    const point=marker.getLatLng();if(point.lat!==v.lat||point.lng!==v.lon)marker.setLatLng([v.lat,v.lon]);
    if(icon)marker.setIcon(icon);
    if(marker.isPopupOpen()||!marker.getPopup()){
    const popup=document.createElement('div');
    popup.append(textNode(`${v.id} · ${v.status} · ${v.source.toUpperCase()}`),document.createElement('br'),textNode(`速度（生値） ${v.speed} / 衛星 ${v.satellites}`),document.createElement('br'),textNode(`受信 ${v.count} 回 / ${v.age_s.toFixed(1)} 秒前`));
    if(marker.getPopup())marker.setPopupContent(popup);else marker.bindPopup(popup);
    }
    let button=rows.get(v.key);
    if(!button){button=document.createElement('button');button.append(textNode(''),textNode(''));button.onclick=()=>select(v.key,true);rows.set(v.key,button);$('vehicles').append(button);}
    button.className='vehicle'+(selected===v.key?' selected':'');
    button.children[0].textContent=`${v.id} · ${v.source.toUpperCase()}`;button.children[1].textContent=`${v.status} · ${v.age_s.toFixed(1)}s`;
  }
  if(!state.vehicles.length)$('vehicles').textContent='受信を待っています';
  const v=state.vehicles.find(v=>v.key===selected);if(!v)return;
  const version=`${v.key}|${v.count}|${Math.floor(v.age_s)}`;if(detailsVersion===version)return;detailsVersion=version;
  const dl=document.createElement('dl');
  const values={'Vehicle':v.id,'入力元':v.source,'状態':v.status,'緯度 / 経度':`${v.lat} / ${v.lon}`,'速度（単位未確認）':v.speed,'衛星数':v.satellites,'最終受信':new Date(v.received_epoch*1000).toLocaleTimeString(),'経過':`${v.age_s.toFixed(1)} 秒${v.age_s>10?'（10秒以上未受信）':''}`,'受信間隔':v.interval_s===null?'—':`${v.interval_s.toFixed(3)} 秒`,'受信回数':v.count};
  v.sensors.forEach((n,i)=>values[`センサー値 ${i+1}`]=n);
  for(const [k,val] of Object.entries(values)){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=val;dl.append(dt,dd);}
  const raw=document.createElement('pre');raw.textContent=v.raw;
  $('details').replaceChildren(dl,raw);
}
let resetting=false;
async function poll(){
  const started=performance.now(), stamp=generation;
  try{
    if(!resetting){const next=await api('/api/state',{signal:AbortSignal.timeout(5000)});
      if(stamp===generation){state=next;$('connection').textContent='● サーバー接続中';render();}}
  }catch(e){$('connection').textContent='● サーバー切断 / 再接続中';$('message').textContent=e.message;}
  finally{setTimeout(poll,Math.max(0,(document.hidden?2000:500)-(performance.now()-started)));}
}
$('reset').onclick=async()=>{
  if(!confirm('現在の受信セッションをリセットします。過去ログは残ります。続けますか？'))return;
  resetting=true;generation++;$('reset').disabled=true;
  try{state=await api('/api/reset',{method:'POST',signal:AbortSignal.timeout(10000)});render();$('message').textContent='リセットしました。新しいログセッションで受信を開始します。';}
  catch(e){$('message').textContent=`リセット結果を確認できません: ${e.message}`;}
  finally{resetting=false;$('reset').disabled=false;}
};
$('fit').onclick=()=>{if(courseLayer&&courseLayer.getBounds().isValid())map.fitBounds(courseLayer.getBounds(),{padding:[25,25]});};
function showCourse(data,name){
  const layer=L.geoJSON(data,{style:f=>({color:f.properties.name.startsWith('SS')?'#da4a43':f.properties.name.includes('ALT')?'#9070ac':'#3b83b8',weight:3}),pointToLayer:(_,latlng)=>L.circleMarker(latlng,{radius:4,color:'#35556c',fillOpacity:0.9}),onEachFeature:(f,l)=>l.bindTooltip(textNode(f.properties.name))});
  if(!layer.getBounds().isValid())throw Error('表示できる座標がありません');
  if(courseLayer)map.removeLayer(courseLayer);
  course=data;courseLayer=layer.addTo(map);
  $('course-name').textContent=name;$('fit').click();
}
function parseKml(xml){
  const doc=new DOMParser().parseFromString(xml,'application/xml');
  if(doc.getElementsByTagName('parsererror').length||doc.documentElement.localName!=='kml')throw Error('正しいKMLファイルではありません');
  const descendants=(node,name)=>Array.from(node.getElementsByTagNameNS('*',name));
  function coordinates(node,min){
    const raw=descendants(node,'coordinates')[0]?.textContent.trim();
    if(!raw)throw Error('座標が空です');
    const result=raw.split(/\s+/).map(token=>{
      const parts=token.split(',');const [lon,lat]=parts.map(Number);
      if(parts.length<2||!parts[0].trim()||!parts[1].trim()||!Number.isFinite(lon)||!Number.isFinite(lat)||Math.abs(lon)>180||Math.abs(lat)>90)throw Error('座標が不正です');
      return [lon,lat];
    });
    if(result.length<min)throw Error('座標数が不足しています');
    return result;
  }
  const features=[];
  for(const pm of descendants(doc,'Placemark')){
    const name=Array.from(pm.children).find(el=>el.localName==='name')?.textContent||'名称なし';
    for(const type of ['Point','LineString','Polygon'])for(const geom of descendants(pm,type)){
      let coords;
      if(type==='Polygon'){
        const outer=descendants(geom,'outerBoundaryIs')[0];
        if(!outer)throw Error('Polygonの外周がありません');
        coords=[outer,...descendants(geom,'innerBoundaryIs')].map(ring=>{
          const points=coordinates(ring,3);
          if(points[0][0]!==points.at(-1)[0]||points[0][1]!==points.at(-1)[1])points.push([...points[0]]);
          return points;
        });
      }else{coords=coordinates(geom,type==='Point'?1:2);if(type==='Point')coords=coords[0];}
      features.push({type:'Feature',properties:{name},geometry:{type,coordinates:coords}});
    }
  }
  if(!features.length)throw Error('Point・LineString・Polygonがありません');
  return {type:'FeatureCollection',features};
}
let courseChoice=0;
$('kml-file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;
  const choice=++courseChoice;
  try{
    if(file.size>20*1024*1024)throw Error('KMLは20MB以下にしてください');
    const data=parseKml(await file.text());
    if(choice!==courseChoice)return;
    showCourse(data,file.name);
    $('message').textContent=`${file.name}：${data.features.length}要素を読み込みました（この画面のみ・再読み込みで初期コースに戻ります）。`;
  }catch(e){$('message').textContent=`KML読み込みエラー: ${e.message}。現在のコースを維持します。`;}
  finally{$('kml-file').value='';}
};
api('/api/course').then(data=>{if(courseChoice===0)showCourse(data,'JRC2SR26 · Leg 1');}).catch(e=>{$('message').textContent=`KML読み込みエラー: ${e.message}`;});
poll();
