import json
from pathlib import Path

def render_final_view():
    p_json = Path(r"d:/project/wikicode/graphify_out/.graphify_pure_merged.json")
    p_js = Path(r"d:/project/wikicode/graphify_out/vis_network_local.js")
    
    if not p_json.exists() or not p_js.exists():
        print("Required files (JSON or JS) missing.")
        return
    
    data = json.loads(p_json.read_text(encoding='utf-8'))
    js_content = p_js.read_text(encoding='utf-8')
    
    # 构建 HTML 头部和尾部
    html_template = """
<!DOCTYPE html>
<html>
<head>
    <title>WikiCoder Expert Intelligence Graph</title>
    <script type="text/javascript">INSERT_JS_HERE</script>
    <style type="text/css">
        body { background: #0b1015; color: #fff; margin: 0; overflow: hidden; font-family: sans-serif; }
        #mynetwork { width: 100vw; height: 100vh; }
        .overlay { position: absolute; top: 20px; left: 20px; z-index: 10; background: rgba(15,25,35,0.9); padding: 15px; border-radius: 8px; border: 1px solid #00ffff; pointer-events: none; }
    </style>
</head>
<body>
<div class="overlay">
    <h3 style="margin:0; color:#00ffff;">🧠 Expert Semantic Network</h3>
    <p style="margin:5px 0 0; font-size:12px; color:#aaa;">Nodes: DATA_NODES_COUNT | Edges: DATA_EDGES_COUNT</p>
    <p style="margin:5px 0 0; font-size:11px; color:#ff9900;">Orange: High-Value Atoms | Cyan: Sources</p>
</div>
<div id="mynetwork"></div>
<script type="text/javascript">
    var nodes = new vis.DataSet(DATA_NODES_JSON);
    var edges = new vis.DataSet(DATA_EDGES_JSON);
    var container = document.getElementById('mynetwork');
    var data = { nodes: nodes, edges: edges };
    var options = {
        nodes: { shape: 'dot', font: { color: '#ffffff', size: 12 }, borderWidth: 1 },
        edges: { width: 0.5, smooth: false, color: '#444' },
        physics: { enabled: true, stabilization: { iterations: 50 }, barnesHut: { gravitationalConstant: -3000 } }
    };
    var network = new vis.Network(container, data, options);
    network.on("stabilizationIterationsDone", function () { network.fit(); });
</script>
</body>
</html>
    """
    
    final_html = html_template.replace("INSERT_JS_HERE", js_content) \
                              .replace("DATA_NODES_JSON", json.dumps(data['nodes'], ensure_ascii=False)) \
                              .replace("DATA_EDGES_JSON", json.dumps(data['edges'], ensure_ascii=False)) \
                              .replace("DATA_NODES_COUNT", str(len(data['nodes']))) \
                              .replace("DATA_EDGES_COUNT", str(len(data['edges'])))
    
    output_path = Path(r"d:/project/wikicode/graphify_out/pure_business_graph.html")
    output_path.write_text(final_html, encoding='utf-8')
    print(f"SUCCESS: Final View rendered at {output_path}")

if __name__ == "__main__":
    render_final_view()
