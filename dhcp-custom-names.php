<?php
ob_start();
// Run against an isolated config directory; never write player configuration.
$source = file_get_contents(__DIR__ . '/../www/api/controllers/proxies.php');
$source = preg_replace('/^<\?\s*require_once[^;]+;/', '', $source);
$source = str_replace('shell_exec(', 'fixtureShellExec(', $source);
eval($source);
function json($data, $unused = false) { return $data; }
function network_list_interfaces_array() { return ['eth0']; }
$leaseIP = '10.10.11.107';
function fixtureShellExec($cmd) {
    global $leaseIP;
    return "Offered DHCP leases: $leaseIP (to AA:BB:CC:DD:EE:FF)\n                     10.10.11.108 (to 11:22:33:44:55:66)\n";
}
$settings = ['mediaDirectory' => sys_get_temp_dir() . '/fpp-names-test-' . uniqid()];
mkdir($settings['mediaDirectory'] . '/config', 0777, true);
function check($value, $message) { if (!$value) throw new Exception($message); echo "PASS: $message\n"; }
function saveNames($changes) { $_POST = $changes; http_response_code(200); return PostDHCPProxyNames(); }
try {
    check(getDHCPLeases() === ['10.10.11.107', '10.10.11.108'], 'Existing IP-only lease interface preserved');
    $result = saveNames([['mac'=>'AA:BB:CC:DD:EE:FF', 'customName'=>' Tree <east> & "stars" 🎄 ']]);
    check($result['status'] === 'OK', 'Save succeeds without mbstring');
    check(LoadProxyList()[0]['customName'] === 'Tree <east> & "stars" 🎄', 'Unicode and punctuation round trip');
    $leaseIP = '10.10.11.140';
    check(LoadProxyList()[0]['host'] === $leaseIP && LoadProxyList()[0]['customName'] === 'Tree <east> & "stars" 🎄', 'Name follows MAC after IP change');
    saveNames([['mac'=>'11:22:33:44:55:66','customName'=>'Garage']]);
    check(count(LoadDHCPProxyNames()) === 2, 'Partial save preserves other devices');
    foreach ([[['mac'=>'invalid','customName'=>'x']], [['mac'=>'aa:bb:cc:dd:ee:ff','customName'=>str_repeat('x',129)]], [['mac'=>'aa:bb:cc:dd:ee:ff','customName'=>"bad\nname"]]] as $invalid) {
        saveNames($invalid); check(http_response_code() === 400, 'Invalid input rejected');
    }
    check(count(LoadDHCPProxyNames()) === 2, 'Rejected input leaves saved data intact');
    saveNames([['mac'=>'aa:bb:cc:dd:ee:ff','customName'=>'']]);
    check(count(LoadDHCPProxyNames()) === 1 && LoadProxyList()[0]['customName'] === '', 'Clearing removes only the selected name');
    check(!file_exists($settings['mediaDirectory'].'/config/proxy-config.conf'), 'Name saves never rewrite proxy configuration');
    file_put_contents($settings['mediaDirectory'].'/config/dhcp-proxy-names.json', 'corrupt');
    saveNames([['mac'=>'aa:bb:cc:dd:ee:ff','customName'=>'No overwrite']]);
    check(http_response_code() === 500 && file_get_contents($settings['mediaDirectory'].'/config/dhcp-proxy-names.json') === 'corrupt', 'Corrupt storage fails without overwriting');
} finally {
    foreach (glob($settings['mediaDirectory'].'/config/*') as $file) unlink($file);
    rmdir($settings['mediaDirectory'].'/config'); rmdir($settings['mediaDirectory']);
}
