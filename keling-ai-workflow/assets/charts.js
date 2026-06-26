// Keling AI workflow — charts & diagrams
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var accent3 = style.getPropertyValue('--accent3').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();

  // ============== Chart 1: 流程耗时占比 ==============
  var chartTime = echarts.init(document.getElementById('chart-time'), null, { renderer: 'svg' });
  chartTime.setOption({
    animation: false,
    tooltip: { trigger: 'item', appendToBody: true, formatter: '{b}: {c}秒 ({d}%)' },
    legend: { bottom: 0, textStyle: { color: muted, fontSize: 12 } },
    color: [accent, accent2, accent3],
    series: [{
      name: '环节耗时',
      type: 'pie',
      radius: ['45%', '70%'],
      center: ['50%', '45%'],
      avoidLabelOverlap: true,
      itemStyle: { borderColor: bg2, borderWidth: 2 },
      label: { color: ink, fontSize: 12, formatter: '{b}\n{c}秒' },
      labelLine: { lineStyle: { color: rule } },
      data: [
        { value: 30, name: '① 导入 PPT' },
        { value: 60, name: '② 添加数字人' },
        { value: 90, name: '③ 生成视频' }
      ]
    }]
  });
  window.addEventListener('resize', function () { chartTime.resize(); });

  // ============== Chart 2: 不同分辨率与页数渲染耗时 ==============
  var chartRender = echarts.init(document.getElementById('chart-render'), null, { renderer: 'svg' });
  chartRender.setOption({
    animation: false,
    tooltip: { trigger: 'axis', appendToBody: true, valueFormatter: function (v) { return v + ' 秒'; } },
    legend: { data: ['横屏 1080P', '横屏 720P', '竖屏 1080P', '方屏 1:1'], textStyle: { color: muted, fontSize: 12 }, top: 0 },
    grid: { top: 50, left: 50, right: 30, bottom: 40 },
    xAxis: {
      type: 'category',
      data: ['10 页', '20 页', '40 页', '60 页', '80 页'],
      axisLine: { lineStyle: { color: rule } },
      axisLabel: { color: muted, fontSize: 12 }
    },
    yAxis: {
      type: 'value',
      name: '渲染耗时 (秒)',
      nameTextStyle: { color: muted, fontSize: 12 },
      axisLine: { lineStyle: { color: rule } },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } },
      axisLabel: { color: muted, fontSize: 12 }
    },
    color: [accent, accent2, accent3, '#ffb454'],
    series: [
      {
        name: '横屏 1080P', type: 'bar', barGap: '10%',
        data: [50, 90, 170, 240, 320],
        itemStyle: { borderRadius: [4, 4, 0, 0] }
      },
      {
        name: '横屏 720P', type: 'bar',
        data: [35, 60, 110, 160, 215],
        itemStyle: { borderRadius: [4, 4, 0, 0] }
      },
      {
        name: '竖屏 1080P', type: 'bar',
        data: [55, 90, 175, 250, 335],
        itemStyle: { borderRadius: [4, 4, 0, 0] }
      },
      {
        name: '方屏 1:1', type: 'bar',
        data: [45, 75, 140, 200, 270],
        itemStyle: { borderRadius: [4, 4, 0, 0] }
      }
    ]
  });
  window.addEventListener('resize', function () { chartRender.resize(); });

  // ============== Mermaid: 整体流程图 ==============
  if (window.mermaid) {
    mermaid.initialize({
      startOnLoad: true,
      theme: 'base',
      securityLevel: 'loose',
      themeVariables: {
        primaryColor: '#181c3a',
        primaryTextColor: '#f4f5fb',
        primaryBorderColor: '#6c8cff',
        lineColor: '#6c8cff',
        secondaryColor: '#232851',
        tertiaryColor: '#0f1226',
        fontFamily: 'InstrumentSans, PingFang SC, Microsoft YaHei, sans-serif'
      }
    });
  }
})();
