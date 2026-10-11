package com.tongfa.photoimport;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.ImageFormat;
import android.hardware.Camera;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.TextUtils;
import android.util.AttributeSet;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.AdapterView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * 光猫补录（连拍版）：内置取景框，按一次快门=存一张并自动后台上传，马上可以拍下一张。
 * 顶部选择目标文件夹，底部实时显示"已上传 N 台 / 该目录共 X 台"。
 */
public class MainActivity extends Activity implements SurfaceHolder.Callback {
    private static final int REQ_PICK = 2;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final Object pumpLock = new Object();
    private final ArrayDeque<Item> queue = new ArrayDeque<Item>();
    private final List<Item> failed = Collections.synchronizedList(new ArrayList<Item>());
    private volatile boolean working = false;

    /** 上传队列里的一项：照片文件 + 该进的目录（拍照/选图那一刻定死，之后切目录不影响它） */
    private static class Item {
        final File file;
        final String folder;
        Item(File f, String folder) { this.file = f; this.folder = folder; }
    }

    private SharedPreferences prefs;
    private EditText serverEdit;
    private Spinner folderSpinner;
    private ArrayAdapter<String> folderAdapter;
    private AspectSurfaceView previewView;
    private TextView stats;
    private TextView hint;
    private Button shutter;

    private Camera camera;
    private boolean focusBeforeShot = false;   // 非连续对焦的机型：拍之前先对一次焦
    private volatile boolean shotInFlight = false;
    private boolean spinnerReady = false;
    private Spinner sizeSpinner;
    private ArrayAdapter<String> sizeAdapter;
    private final List<Camera.Size> sizeList = new ArrayList<Camera.Size>();
    private boolean sizeSpinnerReady = false;
    private boolean sizeListLogged = false;
    private int sessionShots = 0;    // 本次打开 App 拍了多少张
    private int uploaded = 0;       // 本次成功上传多少张
    private volatile int folderTotal = -1;
    private SurfaceHolder pendingHolder;

    // ---- 运行日志：出问题时点「发日志」，整份日志会传给机顶盒，供分析 ----
    private final StringBuilder logBuf = new StringBuilder();
    private volatile boolean logFlushBusy = false;
    private volatile long shootBlockUntil = 0L;   // 拍照超时后的冷却，防止连点
    private volatile boolean cameraRebuilding = false;
    private volatile boolean rebuildAfterShot = false;   // 相机一旦卡过一次，就改成"每拍完一张重建相机"

    /** 一次"按快门"的状态：卡住重拍时，防止旧回调重复存图 */
    private static class Shot {
        boolean saved = false;         // 本次按快门已保存过照片
        boolean retryPending = false;  // 正在自动重启相机重拍
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("photo_import", MODE_PRIVATE);
        loadLogBuf();
        logEvent("===== App 启动 v2.0 (vc11) 手机=" + Build.MODEL
                + " 安卓=" + Build.VERSION.RELEASE);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(10);
        root.setPadding(pad, pad, pad, pad);

        LinearLayout row0 = new LinearLayout(this);
        row0.setOrientation(LinearLayout.HORIZONTAL);
        serverEdit = new EditText(this);
        // 默认地址是占位示例：首次使用请改成自己的机顶盒地址（改完会自动记住）
        serverEdit.setText(prefs.getString("server", "http://<机顶盒IP>:8090"));
        serverEdit.setSingleLine(true);
        serverEdit.setTextSize(12);
        row0.addView(serverEdit, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button refresh = new Button(this);
        refresh.setText("刷新目录");
        refresh.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { loadFolders(); }
        });
        row0.addView(refresh);
        root.addView(row0);

        LinearLayout row1 = new LinearLayout(this);
        row1.setOrientation(LinearLayout.HORIZONTAL);
        TextView lb = new TextView(this);
        lb.setText("补录到：");
        row1.addView(lb);
        folderSpinner = new Spinner(this);
        folderAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_dropdown_item, new ArrayList<String>());
        folderSpinner.setAdapter(folderAdapter);
        folderSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            public void onItemSelected(AdapterView<?> parent, View view, int pos, long id) {
                if (!spinnerReady) return;
                logEvent("选择目录: " + (folderSpinner.getSelectedItem() == null
                        ? "" : folderSpinner.getSelectedItem().toString()));
                startNewBatch();
            }

            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
        row1.addView(folderSpinner, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(row1);

        previewView = new AspectSurfaceView(this);
        previewView.getHolder().addCallback(this);
        root.addView(previewView, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        stats = new TextView(this);
        stats.setTextSize(15);
        stats.setText("拍 0 张 ｜ 已上传 0 台 ｜ 该目录 ? 台 ｜ 待传 0");
        root.addView(stats);

        hint = new TextView(this);
        hint.setTextSize(12);
        hint.setText("v2.0 ｜ 对着标签按「拍照」即可，拍完自动上传、马上能拍下一张");
        root.addView(hint);

        LinearLayout rowSize = new LinearLayout(this);
        rowSize.setOrientation(LinearLayout.HORIZONTAL);
        TextView lbSize = new TextView(this);
        lbSize.setText("拍照尺寸（同预览）：");
        rowSize.addView(lbSize);
        sizeSpinner = new Spinner(this);
        sizeAdapter = new ArrayAdapter<String>(this,
                android.R.layout.simple_spinner_dropdown_item, new ArrayList<String>());
        sizeSpinner.setAdapter(sizeAdapter);
        sizeSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            public void onItemSelected(AdapterView<?> parent, View view, int pos, long id) {
                if (!sizeSpinnerReady) return;
                if (pos < 0 || pos >= sizeList.size()) return;
                Camera.Size s = sizeList.get(pos);
                prefs.edit().putString("pic_size", s.width + "x" + s.height).apply();
                logEvent("用户选择拍照尺寸: " + s.width + "x" + s.height);
                applyPictureSize(s);
            }

            public void onNothingSelected(AdapterView<?> parent) {
            }
        });
        rowSize.addView(sizeSpinner, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(rowSize);

        LinearLayout row2 = new LinearLayout(this);
        row2.setOrientation(LinearLayout.HORIZONTAL);
        Button pick = new Button(this);
        pick.setText("相册");
        pick.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { pickPhoto(); }
        });
        row2.addView(pick, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button retry = new Button(this);
        retry.setText("重传失败");
        retry.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { retryFailed(); }
        });
        row2.addView(retry, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button clear = new Button(this);
        clear.setText("清空本批");
        clear.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { clearBatch(); }
        });
        row2.addView(clear, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button sendlog = new Button(this);
        sendlog.setText("发日志");
        sendlog.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                toast("正在发送日志…");
                flushLog("手动");
            }
        });
        row2.addView(sendlog, new LinearLayout.LayoutParams(0,
                ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        root.addView(row2);

        shutter = new Button(this);
        shutter.setText("拍  照");
        shutter.setTextSize(20);
        shutter.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { shoot(); }
        });
        root.addView(shutter, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(72)));

        setContentView(root);
        loadFolders();
        askCameraPermission();
    }

    private void askCameraPermission() {
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.CAMERA}, 100);
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (code == 100) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) {
                if (pendingHolder != null) openCamera(pendingHolder);
            } else {
                logEvent("没有相机权限，等待用户在系统设置里开启");
                hint.setText("没有相机权限：请在 系统设置-应用-光猫补录-权限 里打开「相机」后重开 App");
            }
        }
    }

    // ---------------- 相机 ----------------

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        pendingHolder = holder;
        logEvent("预览窗口创建");
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            askCameraPermission();
            return;
        }
        openCamera(holder);
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        if (camera != null) {
            try {
                camera.stopPreview();
                camera.setPreviewDisplay(holder);
                camera.startPreview();
            } catch (Exception ignored) {
            }
        }
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        logEvent("预览窗口销毁");
        releaseCamera();
    }

    private void openCamera(SurfaceHolder holder) {
        if (camera != null) {
            logEvent("openCamera 跳过：相机已打开");
            return;
        }
        cameraRebuilding = false;
        try {
            int id = -1;
            for (int i = 0; i < Camera.getNumberOfCameras(); i++) {
                Camera.CameraInfo info = new Camera.CameraInfo();
                Camera.getCameraInfo(i, info);
                if (info.facing == Camera.CameraInfo.CAMERA_FACING_BACK) { id = i; break; }
            }
            camera = Camera.open(id >= 0 ? id : 0);
            camera.setErrorCallback(new Camera.ErrorCallback() {
                public void onError(int error, Camera c2) {
                    logEvent("相机错误回调: code=" + error + "，重建相机");
                    rebuildCamera("错误回调" + error);
                }
            });
            Camera.Parameters p = camera.getParameters();
            p.setPictureFormat(ImageFormat.JPEG);
            p.setJpegQuality(95);
            Camera.Size pic = null;
            // 拍照尺寸：按面积降序（去重），提供 4:3 与 16:9 两种档位，用户可下拉改
            List<Camera.Size> all = new ArrayList<Camera.Size>(p.getSupportedPictureSizes());
            Collections.sort(all, new Comparator<Camera.Size>() {
                public int compare(Camera.Size a, Camera.Size b) {
                    long aa = (long) a.width * a.height;
                    long bb = (long) b.width * b.height;
                    if (aa != bb) return aa > bb ? -1 : 1;
                    return a.width == b.width ? 0 : (a.width > b.width ? -1 : 1);
                }
            });
            if (!sizeListLogged) {
                StringBuilder sb = new StringBuilder("支持拍照尺寸:");
                for (Camera.Size s : all) {
                    sb.append(" ").append(s.width).append("x").append(s.height);
                }
                logEvent(sb.toString());
                StringBuilder sb2 = new StringBuilder("支持预览尺寸:");
                for (Camera.Size s : p.getSupportedPreviewSizes()) {
                    sb2.append(" ").append(s.width).append("x").append(s.height);
                }
                logEvent(sb2.toString());
                sizeListLogged = true;
            }
            List<Camera.Size> uniq = new ArrayList<Camera.Size>();
            for (Camera.Size s : all) {
                boolean dup = false;
                for (Camera.Size u : uniq) {
                    if (u.width == s.width && u.height == s.height) { dup = true; break; }
                }
                // 只保留 4:3 和 16:9 两种常用档位；
                // 预览宽高比会跟随所选尺寸自动切换，保证"看到的就是拍到的"
                if (dup) continue;
                boolean is43 = Math.abs((long) s.width * 3 - (long) s.height * 4) < 8;
                boolean is169 = Math.abs(s.width * 9.0 / 16.0 - s.height) < 8;
                if (is43 || is169) uniq.add(s);
            }
            if (uniq.isEmpty()) {
                logEvent("没有可用的标准比例尺寸，退回全部尺寸");
                for (Camera.Size s : all) {
                    boolean dup = false;
                    for (Camera.Size u : uniq) {
                        if (u.width == s.width && u.height == s.height) { dup = true; break; }
                    }
                    if (!dup) uniq.add(s);
                }
            } else {
                logEvent("拍照尺寸：4:3 + 16:9 共 " + uniq.size() + " 档");
            }
            String want = prefs.getString("pic_size", "");
            if (!TextUtils.isEmpty(want)) {
                for (Camera.Size s : uniq) {
                    if ((s.width + "x" + s.height).equals(want)) { pic = s; break; }
                }
            }
            if (pic == null && !uniq.isEmpty()) {
                Camera.Size best169 = null;
                for (Camera.Size s : uniq) {
                    if (Math.abs(s.width * 9.0 / 16.0 - s.height) < 8
                            && (best169 == null
                            || (long) s.width * s.height > (long) best169.width * best169.height)) {
                        best169 = s;
                    }
                }
                pic = best169 != null ? best169 : uniq.get(0);
                logEvent("拍照尺寸默认取最大 16:9: " + pic.width + "x" + pic.height);
            } else if (pic != null) {
                logEvent("拍照尺寸沿用上次选择: " + pic.width + "x" + pic.height);
            }
            if (pic != null) p.setPictureSize(pic.width, pic.height);
            // 预览尺寸跟随拍照尺寸的宽高比：选 16:9 就预览 16:9、选 4:3 就预览 4:3
            Camera.Size pv = null;
            if (pic != null) {
                float picRatio = (float) pic.height / pic.width;   // 高/宽
                for (Camera.Size s : p.getSupportedPreviewSizes()) {
                    if (s.width <= 1600 && Math.abs((float) s.height / s.width - picRatio) < 0.02) {
                        if (pv == null || s.width > pv.width) pv = s;
                    }
                }
            }
            if (pv == null) {   // 万一没有同比例的预览尺寸，退回 4:3 预览
                for (Camera.Size s : p.getSupportedPreviewSizes()) {
                    if (s.width <= 1600 && Math.abs((long) s.width * 3 - (long) s.height * 4) < 8) {
                        if (pv == null || s.width > pv.width) pv = s;
                    }
                }
            }
            if (pv != null) p.setPreviewSize(pv.width, pv.height);
            float viewRatio = pv != null ? (float) pv.height / pv.width : 4f / 3f;
            logEvent("预览尺寸: " + (pv == null ? "默认" : pv.width + "x" + pv.height));
            previewView.setAspect(viewRatio);
            final Camera.Size chosenForUi = pic;
            sizeList.clear();
            sizeList.addAll(uniq);
            sizeSpinnerReady = false;
            sizeAdapter.clear();
            for (Camera.Size s : uniq) {
                boolean is169 = Math.abs(s.width * 9.0 / 16.0 - s.height) < 8;
                sizeAdapter.add(s.width + "x" + s.height + " ("
                        + Math.round((long) s.width * s.height / 1000000.0) + "MP "
                        + (is169 ? "16:9" : "4:3") + ")");
            }
            int sel = 0;
            for (int i = 0; i < uniq.size(); i++) {
                if (chosenForUi != null && uniq.get(i) == chosenForUi) { sel = i; break; }
            }
            sizeSpinner.setSelection(sel);
            sizeSpinnerReady = true;
            // 对焦模式：优先连续对焦（预览+拍照都自动对焦），否则拍前手动对一次
            List<String> focusModes = p.getSupportedFocusModes();
            if (focusModes != null && focusModes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE)) {
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
                focusBeforeShot = false;
            } else if (focusModes != null && focusModes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO)) {
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_VIDEO);
                focusBeforeShot = false;
            } else if (focusModes != null && focusModes.contains(Camera.Parameters.FOCUS_MODE_AUTO)) {
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_AUTO);
                focusBeforeShot = true;
            } else {
                focusBeforeShot = false;
            }
            camera.setParameters(p);
            camera.setDisplayOrientation(90);
            camera.setPreviewDisplay(holder);
            camera.startPreview();
            hint.setText("相机就绪 ｜ 对焦模式 " + p.getFocusMode()
                    + " ｜ 拍照 " + p.getPictureSize().width + "x" + p.getPictureSize().height);
            logEvent("相机就绪: 拍照 " + p.getPictureSize().width + "x" + p.getPictureSize().height
                    + " 预览 " + (pv == null ? "默认" : pv.width + "x" + pv.height)
                    + " 对焦 " + p.getFocusMode() + " 拍前手动对焦=" + focusBeforeShot);
        } catch (Exception e) {
            logEvent("打开相机失败: " + e);
            toast("打开相机失败: " + e);
        }
    }

    private void releaseCamera() {
        if (camera != null) {
            try {
                camera.stopPreview();
                camera.release();
            } catch (Exception ignored) {
            }
            camera = null;
        }
    }

    /** 用户改了拍照尺寸：重建一次相机，让拍照尺寸和预览宽高比一起生效 */
    private void applyPictureSize(Camera.Size s) {
        logEvent("切换拍照尺寸: " + s.width + "x" + s.height + "（重建相机，预览一起切）");
        rebuildCamera("切换尺寸");
    }

    @Override
    protected void onPause() {
        super.onPause();
        logEvent("onPause：释放相机");
        releaseCamera();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (previewView.getHolder().getSurface() != null && camera == null) {
            openCamera(previewView.getHolder());
        }
    }

    private void shoot() {
        if (camera == null) {
            toast(cameraRebuilding ? "相机正在重启，1 秒后再按" : "相机还没准备好");
            return;
        }
        if (TextUtils.isEmpty(selectedFolder())) {
            toast("先选择目标文件夹（点刷新目录）");
            return;
        }
        if (shotInFlight) {
            logEvent("点击被忽略：上一张还在拍");
            toast("上一张还在拍，稍等一下再按");
            return;
        }
        if (System.currentTimeMillis() < shootBlockUntil) {
            logEvent("点击被忽略：超时后的冷却中");
            toast("相机忙，1 秒后再按");
            return;
        }
        shotInFlight = true;
        final Camera cam = camera;
        final String targetDir = selectedFolder();   // 按快门这一刻就把目录定死
        final Shot shot = new Shot();
        logEvent("按下拍照: 目录=" + targetDir);
        if (!focusBeforeShot) {
            takePictureAttempt(cam, targetDir, 0, shot);
            return;
        }
        // 非连续对焦机型：先对一次焦，成功或 1.5 秒超时后再拍
        final boolean[] done = {false};
        final Runnable takeNow = new Runnable() {
            public void run() {
                if (done[0]) return;
                done[0] = true;
                takePictureAttempt(cam, targetDir, 0, shot);
            }
        };
        try {
            cam.autoFocus(new Camera.AutoFocusCallback() {
                public void onAutoFocus(boolean success, Camera c) {
                    logEvent("对焦回调: " + success);
                    ui.post(takeNow);
                }
            });
            ui.postDelayed(takeNow, 1500);
        } catch (Exception e) {
            logEvent("对焦调用异常: " + e);
            takeNow.run();
        }
    }

    /** 重建相机：释放后 ~0.3 秒重开（该机型同一台相机第二次拍照必卡，需拍完就重启） */
    private void rebuildCamera(String why) {
        logEvent("重建相机(" + why + ")");
        cameraRebuilding = true;
        releaseCamera();
        ui.postDelayed(new Runnable() {
            public void run() {
                if (previewView.getHolder().getSurface() != null && camera == null) {
                    openCamera(previewView.getHolder());
                } else if (camera == null) {
                    cameraRebuilding = false;
                }
            }
        }, 300);
    }

    /** 等相机重新打开后执行（最多等 5 秒，每 250ms 查一次） */
    private void whenCameraReady(final Runnable action, final int triesLeft) {
        if (camera != null) {
            action.run();
            return;
        }
        if (triesLeft <= 0) {
            logEvent("等待相机就绪超时");
            shotInFlight = false;
            hint.setText("相机重启失败，请重开一下 App");
            return;
        }
        ui.postDelayed(new Runnable() {
            public void run() {
                whenCameraReady(action, triesLeft - 1);
            }
        }, 250);
    }

    /** attempt：第几次尝试（0=用户按的那次）。卡住会自动重启相机并重拍 */
    private void takePictureAttempt(final Camera cam, final String targetDir,
                                    final int attempt, final Shot shot) {
        final boolean[] finished = {false};
        final long t0 = System.currentTimeMillis();
        String sz = "?";
        String fr = "?";
        try {
            Camera.Size s = cam.getParameters().getPictureSize();
            sz = s.width + "x" + s.height;
            long freeByte = Runtime.getRuntime().maxMemory()
                    - Runtime.getRuntime().totalMemory() + Runtime.getRuntime().freeMemory();
            fr = (freeByte / (1024 * 1024)) + "MB";
        } catch (Exception ignored) {
        }
        final String sizeStr = sz;
        final String freeStr = fr;
        // 实测这台 vivo：同一台相机拍第二张必卡（回调永远不来）-> 3 秒没动静就重启相机自动重拍
        final Runnable watchdog = new Runnable() {
            public void run() {
                if (finished[0]) return;
                finished[0] = true;
                logEvent("拍照卡住: 3 秒无回调 (第 " + (attempt + 1) + " 次尝试, size=" + sizeStr
                        + " 内存=" + freeStr + ")");
                if (attempt < 2) {
                    shot.retryPending = true;
                    if (!rebuildAfterShot) {
                        logEvent("相机卡顿一次: 开启「拍完自动重建相机」模式");
                    }
                    rebuildAfterShot = true;
                    hint.setText("相机没响应，正在自动重启重拍…");
                    rebuildCamera("卡住重拍");
                    whenCameraReady(new Runnable() {
                        public void run() {
                            takePictureAttempt(camera, targetDir, attempt + 1, shot);
                        }
                    }, 20);
                } else {
                    shot.retryPending = false;
                    rebuildAfterShot = true;
                    shotInFlight = false;
                    shootBlockUntil = System.currentTimeMillis() + 2000;
                    logEvent("拍照失败: 重启重拍两次都没有回调");
                    hint.setText("相机连续没响应，已重启相机，请等 2 秒再按一次");
                    rebuildCamera("连续失败");
                }
            }
        };
        try {
            hint.setText(attempt == 0 ? "正在写入 ..." : "正在重拍 ...");
            logEvent("takePicture 调用: " + sizeStr + " 剩余内存 " + freeStr
                    + (attempt > 0 ? " (第 " + (attempt + 1) + " 次)" : ""));
            ui.postDelayed(watchdog, 3000);
            cam.takePicture(null, null, new Camera.PictureCallback() {
                public void onPictureTaken(byte[] data, Camera camera) {
                    boolean late = finished[0];
                    if (late && shot.saved) {
                        logEvent("拍照回调(迟到, 本次已存过图, 丢弃)");
                        return;
                    }
                    if (late && shot.retryPending) {
                        logEvent("拍照回调(迟到, 正在重拍, 丢弃)");
                        return;
                    }
                    finished[0] = true;
                    ui.removeCallbacks(watchdog);
                    long dt = System.currentTimeMillis() - t0;
                    logEvent("拍照回调: 用时 " + dt + "ms, "
                            + (data == null ? "data=null(相机失败)" : (data.length / 1024) + "KB")
                            + (late ? " [迟到, 补存]" : ""));
                    try {
                        if (data == null) {
                            ui.post(new Runnable() {
                                public void run() {
                                    hint.setText("相机返回了空图，请再按一次拍照");
                                }
                            });
                            return;
                        }
                        File dir = new File(getCacheDir(), "capture");
                        if (!dir.exists()) dir.mkdirs();
                        File f = new File(dir, "shot_" + System.currentTimeMillis() + ".jpg");
                        FileOutputStream os = new FileOutputStream(f);
                        os.write(data);
                        os.close();
                        sessionShots++;
                        shot.saved = true;
                        shot.retryPending = false;
                        int qn;
                        synchronized (queue) { queue.add(new Item(f, targetDir)); qn = queue.size(); }
                        logEvent("已存队列: " + f.getName() + " (" + (data.length / 1024)
                                + "KB) 排队 " + qn + " 目录=" + targetDir);
                        updateStats();
                        pump();
                        ui.post(new Runnable() {
                            public void run() {
                                hint.setText("已拍 " + sessionShots + " 张，正在上传…（可继续拍）");
                            }
                        });
                    } catch (Exception e) {
                        logEvent("保存照片失败: " + e);
                        toast("保存照片失败: " + e);
                    } finally {
                        shotInFlight = false;
                        if (rebuildAfterShot) {
                            // 这台相机出现过"第二枪哑火"：每拍完一张就重建，保证下一枪是"第一张"
                            rebuildCamera("拍完预重建");
                        } else {
                            try {
                                cam.startPreview();   // 正常状态：立刻恢复取景，接着拍下一张
                            } catch (Exception ignored) {
                            }
                        }
                    }
                }
            });
        } catch (Exception e) {
            finished[0] = true;
            ui.removeCallbacks(watchdog);
            shotInFlight = false;
            logEvent("takePicture 抛异常: " + e);
            toast("拍照失败: " + e);
        }
    }

    private void pickPhoto() {
        try {
            Intent it = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            it.addCategory(Intent.CATEGORY_OPENABLE);
            it.setType("image/*");
            startActivityForResult(it, REQ_PICK);
        } catch (Exception e) {
            toast("打开相册失败: " + e);
        }
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != REQ_PICK || res != RESULT_OK || data == null || data.getData() == null) return;
        final Uri uri = data.getData();
        final String folder = selectedFolder();
        if (TextUtils.isEmpty(folder)) {
            toast("先选择目标文件夹");
            return;
        }
        new Thread(new Runnable() {
            public void run() {
                try {
                    InputStream in = getContentResolver().openInputStream(uri);
                    File dir = new File(getCacheDir(), "capture");
                    if (!dir.exists()) dir.mkdirs();
                    File f = new File(dir, "pick_" + System.currentTimeMillis() + ".jpg");
                    FileOutputStream os = new FileOutputStream(f);
                    byte[] buf = new byte[8192];
                    int n;
                    while ((n = in.read(buf)) > 0) os.write(buf, 0, n);
                    os.close();
                    in.close();
                    sessionShots++;
                    int qn;
                    synchronized (queue) { queue.add(new Item(f, folder)); qn = queue.size(); }
                    logEvent("相册入队: " + f.getName() + " 目录=" + folder + " 排队 " + qn);
                    updateStats();
                    pump();
                } catch (Exception e) {
                    logEvent("读取相册失败: " + e);
                    toast("读取相册失败: " + e);
                }
            }
        }).start();
    }

    // ---------------- 上传队列 ----------------

    private void pump() {
        synchronized (pumpLock) {
            if (working) return;
            working = true;
        }
        new Thread(new Runnable() {
            public void run() {
                logEvent("上传线程启动");
                try {
                    while (true) {
                        final Item it;
                        synchronized (queue) {
                            it = queue.poll();
                        }
                        if (it == null) break;
                        boolean ok = false;
                        try {
                            for (int i = 0; i < 3 && !ok; i++) {
                                ok = uploadOne(it);
                                if (!ok) {
                                    try { Thread.sleep(2000); } catch (InterruptedException ignored) { }
                                }
                            }
                        } catch (Throwable t) {
                            // 单张照片上传时出任何错都不能拖垮整个队列
                            final String msg = "上传出错: " + t;
                            logEvent("单张上传异常: " + t);
                            ui.post(new Runnable() {
                                public void run() { hint.setText(msg); }
                            });
                            ok = false;
                        }
                        if (ok) {
                            uploaded++;
                            //noinspection ResultOfMethodCallIgnored
                            it.file.delete();
                            final String name = it.file.getName();
                            logEvent("上传成功: " + it.folder + "/" + name);
                            ui.post(new Runnable() {
                                public void run() { hint.setText("已上传：" + name); }
                            });
                        } else {
                            failed.add(it);
                            final String name = it.file.getName();
                            logEvent("上传失败: " + it.folder + "/" + name + "（进重传列表）");
                            ui.post(new Runnable() {
                                public void run() {
                                    hint.setText("上传失败：" + name + "（已进「重传失败」列表）");
                                }
                            });
                        }
                        updateStats();
                    }
                    int failN2;
                    synchronized (failed) { failN2 = failed.size(); }
                    boolean clean = failN2 == 0;
                    logEvent("队列传完: 上传成功共 " + uploaded + " 张，失败 " + failN2 + " 张");
                    if (clean) {
                        ui.post(new Runnable() {
                            public void run() {
                                hint.setText("本批全部传完（已上传 " + uploaded
                                        + " 台）｜ 可继续拍，或「清空本批」换目录");
                            }
                        });
                        flushLog("队列传完");
                    }
                } catch (Throwable t) {
                    // 关键：哪怕上传线程因内存不足等崩掉，也要把 working 复位，
                    // 否则队列会永久卡住（现场表现为一直"正在写入"）
                    final String msg = "上传线程异常: " + t;
                    logEvent("上传线程异常: " + t);
                    ui.post(new Runnable() {
                        public void run() { hint.setText(msg); }
                    });
                } finally {
                    synchronized (pumpLock) { working = false; }
                    boolean more;
                    synchronized (queue) { more = !queue.isEmpty(); }
                    if (more) pump();   // 兜底：万一复位瞬间又有新照片进队列
                }
            }
        }).start();
    }

    private boolean uploadOne(Item it) {
        HttpURLConnection c = null;
        File f = it.file;
        long t0 = System.currentTimeMillis();
        try {
            String url = server() + "/upload?folder="
                    + URLEncoder.encode(it.folder, "UTF-8")
                    + "&ts=" + System.currentTimeMillis();
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setRequestMethod("POST");
            c.setDoOutput(true);
            c.setConnectTimeout(8000);
            c.setReadTimeout(180000);
            c.setRequestProperty("Content-Type", "image/jpeg");
            c.setFixedLengthStreamingMode(f.length());
            OutputStream os = c.getOutputStream();
            InputStream fis = new java.io.FileInputStream(f);
            byte[] chunk = new byte[65536];
            int m;
            while ((m = fis.read(chunk)) > 0) os.write(chunk, 0, m);
            fis.close();
            os.close();
            int code = c.getResponseCode();
            InputStream in = code >= 400 ? c.getErrorStream() : c.getInputStream();
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            byte[] buf = new byte[4096];
            int n;
            if (in != null) {
                while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
                in.close();
            }
            String body = new String(bos.toByteArray(), "UTF-8");
            long dt = System.currentTimeMillis() - t0;
            logEvent("上传响应: HTTP " + code + " 用时 " + dt + "ms " + f.getName()
                    + " (" + (f.length() / 1024) + "KB) "
                    + (body.length() > 120 ? body.substring(0, 120) : body));
            if (code != 200) {
                final String msg = "上传被拒(HTTP " + code + "): " + body;
                ui.post(new Runnable() {
                    public void run() { hint.setText(msg); }
                });
                return false;
            }
            JSONObject o = new JSONObject(body);
            final int cnt = o.optInt("count", -1);
            final String name = o.optString("name");
            ui.post(new Runnable() {
                public void run() {
                    if (cnt >= 0) folderTotal = cnt;
                    hint.setText("已上传 " + name + "（该目录共 " + folderTotal + " 台）");
                }
            });
            return true;
        } catch (final Exception e) {
            logEvent("上传异常: " + f.getName() + " " + e);
            ui.post(new Runnable() {
                public void run() { hint.setText("上传出错: " + e); }
            });
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private void retryFailed() {
        int n;
        synchronized (failed) {
            n = failed.size();
            synchronized (queue) {
                queue.addAll(failed);
            }
            failed.clear();
        }
        if (n > 0) logEvent("重传失败列表: " + n + " 张重新入队");
        updateStats();
        pump();
    }

    /** 开始新一批：清空计数、重新取该目录已有台数 */
    private void startNewBatch() {
        sessionShots = 0;
        uploaded = 0;
        folderTotal = -1;
        updateStats();
        String folder = selectedFolder();
        if (!TextUtils.isEmpty(folder)) {
            logEvent("开始新批次: 目录=" + folder);
            hint.setText("当前批次：" + folder);
            fetchCount(folder);
        }
    }

    /** 清空按钮：本批补完、准备换目录时用（有没传完的会先拦住） */
    private void clearBatch() {
        int pend;
        int failN;
        synchronized (queue) {
            pend = queue.size();
        }
        synchronized (failed) {
            failN = failed.size();
        }
        if (pend > 0 || failN > 0) {
            logEvent("清空被拦: 还有 " + (pend + failN) + " 张没传完");
            toast("还有 " + (pend + failN) + " 张没传完：等它传完，或点「重传失败」");
            return;
        }
        startNewBatch();
        logEvent("已清空本批计数");
        toast("已清空本批计数，可以切换目录继续补录");
    }

    private void fetchCount(final String folder) {
        new Thread(new Runnable() {
            public void run() {
                try {
                    String url = server() + "/count?folder=" + URLEncoder.encode(folder, "UTF-8");
                    HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                    c.setConnectTimeout(5000);
                    c.setReadTimeout(8000);
                    InputStream in = c.getInputStream();
                    ByteArrayOutputStream bos = new ByteArrayOutputStream();
                    byte[] buf = new byte[2048];
                    int n;
                    while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
                    in.close();
                    JSONObject o = new JSONObject(new String(bos.toByteArray(), "UTF-8"));
                    final int cnt = o.optInt("count", -1);
                    logEvent("台数查询: " + folder + " -> " + cnt);
                    ui.post(new Runnable() {
                        public void run() {
                            // 只有还选着这个目录时才更新台数，防止切目录后旧数据覆盖
                            if (folder.equals(selectedFolder())) {
                                folderTotal = cnt;
                                updateStats();
                            }
                        }
                    });
                } catch (Exception e) {
                    logEvent("台数查询失败: " + folder + " " + e);
                    // 查不到就先显示 ?，不影响拍照
                }
            }
        }).start();
    }

    // ---------------- 目录 / 状态 ----------------

    private String selectedFolder() {
        Object o = folderSpinner.getSelectedItem();
        return o == null ? "" : o.toString();
    }

    private void loadFolders() {
        final String base = server();
        hint.setText("正在读取目录 ...");
        new Thread(new Runnable() {
            public void run() {
                try {
                    HttpURLConnection c = (HttpURLConnection) new URL(base + "/folders").openConnection();
                    c.setConnectTimeout(5000);
                    c.setReadTimeout(8000);
                    InputStream in = c.getInputStream();
                    ByteArrayOutputStream bos = new ByteArrayOutputStream();
                    byte[] buf = new byte[4096];
                    int n;
                    while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
                    in.close();
                    JSONArray arr = new JSONArray(new String(bos.toByteArray(), "UTF-8"));
                    final List<String> items = new ArrayList<String>();
                    for (int i = 0; i < arr.length(); i++) items.add(arr.getString(i));
                    logEvent("目录加载: " + items.size() + " 个");
                    ui.post(new Runnable() {
                        public void run() {
                            String keep = prefs.getString("folder", "");
                            folderAdapter.clear();
                            folderAdapter.addAll(items);
                            if (!TextUtils.isEmpty(keep)) {
                                int idx = items.indexOf(keep);
                                if (idx >= 0) folderSpinner.setSelection(idx);
                            }
                            spinnerReady = true;
                            startNewBatch();
                            hint.setText(items.isEmpty() ? "服务器上没有可选文件夹"
                                    : "目录已更新（App v2.0）");
                        }
                    });
                } catch (final Exception e) {
                    logEvent("目录加载失败: " + e);
                    ui.post(new Runnable() {
                        public void run() {
                            hint.setText("读取目录失败: " + e + "（检查网络/地址）");
                        }
                    });
                }
            }
        }).start();
    }

    private void updateStats() {
        final int pend;
        final int failN;
        synchronized (queue) {
            pend = queue.size();
        }
        synchronized (failed) {
            failN = failed.size();
        }
        ui.post(new Runnable() {
            public void run() {
                stats.setText("拍 " + sessionShots + " 张 ｜ 已上传 " + uploaded + " 台 ｜ 该目录 "
                        + (folderTotal < 0 ? "?" : String.valueOf(folderTotal)) + " 台 ｜ 待传 "
                        + pend + (failN == 0 ? "" : "（失败 " + failN + "）"));
            }
        });
    }

    private String server() {
        String s = serverEdit.getText().toString().trim();
        if (s.endsWith("/")) s = s.substring(0, s.length() - 1);
        prefs.edit().putString("server", s).apply();
        return s;
    }

    private void toast(final String s) {
        Runnable r = new Runnable() {
            public void run() {
                Toast.makeText(MainActivity.this, s, Toast.LENGTH_SHORT).show();
                hint.setText(s);
            }
        };
        if (Looper.myLooper() == Looper.getMainLooper()) r.run();
        else ui.post(r);
    }

    // ---------------- 运行日志（上传给机顶盒分析） ----------------

    private String nowStr() {
        return new java.text.SimpleDateFormat("MM-dd HH:mm:ss.SSS", java.util.Locale.US)
                .format(new java.util.Date());
    }

    private File logFile() {
        return new File(getFilesDir(), "app_log.txt");
    }

    private void logEvent(String msg) {
        try {
            String line = nowStr() + " " + msg;
            synchronized (logBuf) {
                logBuf.append(line).append('\n');
                if (logBuf.length() > 150000) {
                    int cut = logBuf.indexOf("\n", logBuf.length() - 110000);
                    if (cut > 0) logBuf.delete(0, cut + 1);
                }
            }
            FileOutputStream os = new FileOutputStream(logFile(), true);
            os.write((line + "\n").getBytes("UTF-8"));
            os.close();
            if (logFile().length() > 160000) rewriteLogFile();
        } catch (Exception ignored) {
        }
    }

    private void rewriteLogFile() {
        try {
            String all;
            synchronized (logBuf) { all = logBuf.toString(); }
            FileOutputStream os = new FileOutputStream(logFile());
            os.write(all.getBytes("UTF-8"));
            os.close();
        } catch (Exception ignored) {
        }
    }

    private void loadLogBuf() {
        try {
            File f = logFile();
            if (!f.exists() || f.length() == 0) return;
            int cap = (int) Math.min(f.length(), 150000);
            byte[] buf = new byte[cap];
            java.io.FileInputStream in = new java.io.FileInputStream(f);
            int off = 0;
            int n;
            while (off < cap && (n = in.read(buf, off, cap - off)) > 0) off += n;
            in.close();
            synchronized (logBuf) {
                logBuf.setLength(0);
                logBuf.append(new String(buf, 0, off, "UTF-8"));
            }
        } catch (Exception ignored) {
        }
    }

    /** 把整份日志传给机顶盒（覆盖式，服务器按手机型号各存一份） */
    private void flushLog(final String reason) {
        if (logFlushBusy) return;
        logFlushBusy = true;
        new Thread(new Runnable() {
            public void run() {
                try {
                    String body;
                    synchronized (logBuf) { body = logBuf.toString(); }
                    byte[] data = body.getBytes("UTF-8");
                    String dev = Build.MODEL.replace(' ', '-');
                    HttpURLConnection c = (HttpURLConnection) new URL(server()
                            + "/applog?device=" + URLEncoder.encode(dev, "UTF-8")).openConnection();
                    c.setRequestMethod("POST");
                    c.setDoOutput(true);
                    c.setConnectTimeout(5000);
                    c.setReadTimeout(15000);
                    c.setRequestProperty("Content-Type", "text/plain; charset=utf-8");
                    c.setFixedLengthStreamingMode(data.length);
                    OutputStream os = c.getOutputStream();
                    os.write(data);
                    os.close();
                    int code = c.getResponseCode();
                    c.disconnect();
                    logEvent("日志已发送(" + reason + "): HTTP " + code + " " + data.length + "B");
                } catch (Exception e) {
                    logEvent("日志发送失败(" + reason + "): " + e);
                } finally {
                    logFlushBusy = false;
                }
            }
        }).start();
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    @Override
    protected void onStop() {
        super.onStop();
        prefs.edit().putString("folder", selectedFolder()).apply();
        flushLog("切后台");
    }

    /** 预览控件：宽高比跟随拍照尺寸（4:3 或 16:9），避免画面被拉伸或与成片不一致 */
    public static class AspectSurfaceView extends SurfaceView {
        private float ratioHW = 4f / 3f;   // 高/宽（竖屏：4:3 -> 1.333，16:9 -> 1.778）

        public AspectSurfaceView(Context context) {
            super(context);
        }

        public AspectSurfaceView(Context context, AttributeSet attrs) {
            super(context, attrs);
        }

        public void setAspect(float hOverW) {
            if (Math.abs(hOverW - ratioHW) > 0.01f) {
                ratioHW = hOverW;
                requestLayout();
            }
        }

        @Override
        protected void onMeasure(int widthSpec, int heightSpec) {
            int w = MeasureSpec.getSize(widthSpec);
            int h = MeasureSpec.getSize(heightSpec);
            if (w * ratioHW < h) {
                setMeasuredDimension(w, (int) (w * ratioHW));
            } else {
                setMeasuredDimension((int) (h / ratioHW), h);
            }
        }
    }
}
