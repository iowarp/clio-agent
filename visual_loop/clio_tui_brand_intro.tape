Output "visual_loop/screenshots/clio_tui_brand_intro.gif"

Set Shell "bash"
Set FontSize 14
Set Width 1200
Set Height 760

Hide
Env GACT_CONFIG "/tmp/clio-tui-brand-intro-config.json"
Type "rm -f /tmp/clio-tui-brand-intro-config.json && /tmp/clio-tui-test --backend http://127.0.0.1:9"
Enter
Show

Wait+Screen /press any key/
Screenshot "visual_loop/screenshots/clio_tui_brand_intro.png"
Sleep 300ms
Type "q"
