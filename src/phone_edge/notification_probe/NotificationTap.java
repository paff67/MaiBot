import android.os.Binder;
import android.os.IBinder;
import android.os.Parcel;
import android.os.RemoteException;
import android.os.Looper;
import android.content.ComponentName;
import org.json.JSONObject;
import org.json.JSONArray;
import java.lang.reflect.*;
import android.database.sqlite.SQLiteDatabase;
import android.content.ContentValues;
import java.util.UUID;

/** Probe only: no APK, no WeChat injection, no cancellation/reply operations. */
public class NotificationTap extends Binder implements InvocationHandler {
    private static final String DESCRIPTOR = "android.service.notification.INotificationListener";
    private final Class<?> listenerStub;
    private static SQLiteDatabase queue;
    private static synchronized void persist(JSONObject out) throws Exception {
        out.put("event_uuid", UUID.randomUUID().toString());
        out.put("received_at_ms", System.currentTimeMillis());
        if (queue != null) {
            ContentValues values = new ContentValues();
            values.put("payload", out.toString());
            queue.insertOrThrow("notification_events", null, values);
        }
        System.out.println(out.toString());
    }
    public NotificationTap() throws Exception {
        listenerStub = Class.forName(DESCRIPTOR + "$Stub");
    }
    public Object invoke(Object proxy, Method method, Object[] args) {
        if (method.getName().equals("asBinder")) return this;
        if (method.getName().equals("toString")) return "MaiBotNotificationTap";
        if (method.getName().equals("hashCode")) return System.identityHashCode(this);
        if (method.getName().equals("equals")) return proxy == args[0];
        return null;
    }
    protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
        if (code == INTERFACE_TRANSACTION) {
            if (reply != null) reply.writeString(DESCRIPTOR);
            return true;
        }
        try {
            data.enforceInterface(DESCRIPTOR);
            String name = (String)listenerStub.getMethod("getDefaultTransactionName", int.class).invoke(null, code);
            if ("onListenerConnected".equals(name)) {
                System.out.println("{\"event\":\"connected\"}");
            } else if ("onNotificationPosted".equals(name) || "onNotificationRemoved".equals(name)) {
                IBinder holderBinder = data.readStrongBinder();
                Class<?> holderStub = Class.forName("android.service.notification.IStatusBarNotificationHolder$Stub");
                Object holder = holderStub.getMethod("asInterface", IBinder.class).invoke(null, holderBinder);
                Object sbn = holder.getClass().getMethod("get").invoke(holder);
                emit(name, sbn);
            }
            return true;
        } catch (Throwable e) {
            System.out.println("CALLBACK_ERROR " + e);
            return true;
        }
    }
    private static void emit(String event, Object sbn) throws Exception {
        if (sbn == null) return;
        Class<?> type = sbn.getClass();
        String pkg = (String)type.getMethod("getPackageName").invoke(sbn);
        if (!pkg.equals("com.tencent.mm") && !pkg.equals("com.android.shell")) return;
        JSONObject out = new JSONObject();
        out.put("event", event);
        out.put("package", pkg);
        out.put("key", type.getMethod("getKey").invoke(sbn));
        out.put("notification_id", type.getMethod("getId").invoke(sbn));
        out.put("tag", type.getMethod("getTag").invoke(sbn));
        out.put("android_user_id", type.getMethod("getUserId").invoke(sbn));
        out.put("post_time", type.getMethod("getPostTime").invoke(sbn));
        Object notification = type.getMethod("getNotification").invoke(sbn);
        out.put("notification_flags", notification.getClass().getField("flags").get(notification));
        JSONObject intentFields = new JSONObject();
        try {
            Object pending = notification.getClass().getField("contentIntent").get(notification);
            if (pending != null) {
                Object intent = pending.getClass().getMethod("getIntent").invoke(pending);
                android.os.Bundle bundle = (android.os.Bundle)intent.getClass().getMethod("getExtras").invoke(intent);
                if (bundle != null) {
                    out.put("intent_keys",new JSONArray(bundle.keySet()));
                    for (String key : new String[]{"Chat_User","talker","Main_User","Contact_User","notification_username",
                            "notification_msg_id","notification_create_time","MainUI_User_Last_Msg_Type","talkerCount"}) {
                        Object value = bundle.get(key);
                        if (value instanceof String || value instanceof Number || value instanceof Boolean) intentFields.put(key,value);
                    }
                }
            }
        } catch (Throwable e) { out.put("intent_inspection_error",e.getClass().getSimpleName()); }
        out.put("intent_fields",intentFields);
        android.os.Bundle extras = (android.os.Bundle)notification.getClass().getField("extras").get(notification);
        JSONObject fields = new JSONObject();
        for (String key : extras.keySet()) {
            if (key.equals("android.title") || key.equals("android.text") || key.equals("android.bigText")
                || key.equals("android.conversationTitle") || key.equals("android.subText")) {
                fields.put(key, String.valueOf(extras.get(key)));
            }
        }
        out.put("fields", fields);
        out.put("extra_keys", new JSONArray(extras.keySet()));
        JSONArray messages = new JSONArray();
        android.os.Parcelable[] raw = extras.getParcelableArray("android.messages");
        if (raw != null) {
            for (android.os.Parcelable entry : raw) {
                if (!(entry instanceof android.os.Bundle)) continue;
                android.os.Bundle message = (android.os.Bundle)entry;
                JSONObject item = new JSONObject();
                for (String key : new String[]{"text", "time", "sender", "type", "uri"}) {
                    if (message.containsKey(key)) item.put(key, String.valueOf(message.get(key)));
                }
                messages.put(item);
            }
        }
        out.put("messages", messages);
        persist(out);
    }
    public static void main(String[] args) throws Exception {
        Looper.prepareMainLooper();
        if (args.length > 0) {
            queue = SQLiteDatabase.openOrCreateDatabase(args[0],null);
            queue.enableWriteAheadLogging();
            queue.execSQL("PRAGMA synchronous=FULL");
            queue.execSQL("CREATE TABLE IF NOT EXISTS notification_events (seq INTEGER PRIMARY KEY AUTOINCREMENT,payload TEXT NOT NULL)");
        }
        NotificationTap tap = new NotificationTap();
        Object managerBinder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null,"notification");
        Object manager = Class.forName("android.app.INotificationManager$Stub").getMethod("asInterface",IBinder.class).invoke(null,managerBinder);
        Class<?> listener = Class.forName(DESCRIPTOR);
        Object proxy = Proxy.newProxyInstance(listener.getClassLoader(),new Class<?>[]{listener},tap);
        try {
            manager.getClass().getMethod("registerListener",listener,ComponentName.class,int.class)
                .invoke(manager,proxy,new ComponentName("com.android.shell","MaiBotNotificationTap"),0);
            System.out.println("{\"event\":\"registered\",\"uid\":" + android.os.Process.myUid() + "}");
            try {
                Object snapshot = manager.getClass().getMethod("getActiveNotificationsFromListener",listener,String[].class,int.class)
                    .invoke(manager,proxy,null,0);
                java.util.List<?> active = (java.util.List<?>)snapshot.getClass().getMethod("getList").invoke(snapshot);
                for (Object sbn : active) emit("active_snapshot",sbn);
            } catch (Throwable e) { System.out.println("RECONCILE_FAILED " + e); }
            Looper.loop();
        } catch (InvocationTargetException e) {
            System.out.println("REGISTER_FAILED " + e.getCause());
        }
    }
}
