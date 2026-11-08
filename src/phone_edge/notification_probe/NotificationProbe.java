import java.lang.reflect.*;

/** Zero-APK read-only probe. Never dismisses or opens a notification. */
public class NotificationProbe {
    public static void main(String[] args) throws Exception {
        Object binder = Class.forName("android.os.ServiceManager").getMethod("getService", String.class).invoke(null, "notification");
        Class<?> stub = Class.forName("android.app.INotificationManager$Stub");
        Object manager = stub.getMethod("asInterface", Class.forName("android.os.IBinder")).invoke(null, binder);
        for (Method method : manager.getClass().getMethods()) {
            if (method.getName().equals("registerListener") || method.getName().equals("getActiveNotifications")) {
                System.out.println("API " + method.toString());
            }
        }
        for (Method method : manager.getClass().getMethods()) {
            if (!method.getName().equals("getActiveNotifications")) continue;
            Object[] params = new Object[method.getParameterTypes().length];
            for (int i=0; i<params.length; i++) {
                Class<?> type = method.getParameterTypes()[i];
                params[i] = type == String.class ? "com.android.shell" : type == int.class ? Integer.valueOf(0) : null;
            }
            try {
                Object result = method.invoke(manager, params);
                int size = result == null ? 0 : Array.getLength(result);
                System.out.println("ACTIVE_COUNT " + size);
                for (int i=0; i<size; i++) {
                    Object sbn = Array.get(result,i);
                    String pkg = (String)sbn.getClass().getMethod("getPackageName").invoke(sbn);
                    if (!pkg.equals("com.tencent.mm")) continue;
                    Object notification = sbn.getClass().getMethod("getNotification").invoke(sbn);
                    Object extras = notification.getClass().getField("extras").get(notification);
                    Object keys = extras.getClass().getMethod("keySet").invoke(extras);
                    System.out.println("WECHAT key=" + sbn.getClass().getMethod("getKey").invoke(sbn) + " extras_keys=" + keys);
                }
            } catch (InvocationTargetException e) {
                System.out.println("READ_FAILED " + e.getCause());
            }
        }
    }
}
