package org.nobody.multitts.maintained;

import android.app.Instrumentation;
import android.content.*;
import android.os.*;
import java.io.*;
import java.lang.reflect.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.concurrent.*;
import org.json.*;

/** Only run on the owned resource-verification AVD against the exact signed production APK. */
public final class ScheduledSoundImportInstrumentation extends Instrumentation {
    private int checks;
    private static Object call(Class<?> type,String name,Class<?>[] parameters,Object...args)throws Exception {
        Method method=type.getDeclaredMethod(name,parameters);method.setAccessible(true);return method.invoke(null,args);
    }
    private static Object field(Object value,String name)throws Exception {
        Class<?> type=value instanceof Class?(Class<?>)value:value.getClass();Field f=type.getDeclaredField(name);f.setAccessible(true);return f.get(value instanceof Class?null:value);
    }
    private void require(boolean value,String label){checks++;if(!value)throw new AssertionError(label);}
    private static String sha(byte[] bytes)throws Exception {StringBuilder value=new StringBuilder();for(byte b:MessageDigest.getInstance("SHA-256").digest(bytes))value.append(String.format(Locale.ROOT,"%02x",b&255));return value.toString();}
    private static void erase(File path){File[] children=path.listFiles();if(children!=null)for(File child:children)erase(child);if(!path.delete()&&path.exists())throw new IllegalStateException("Could not clean owned test data");}
    @Override public void onCreate(Bundle arguments){super.onCreate(arguments);start();}
    @Override public void onStart(){
        Context app=getTargetContext();JSONObject report=new JSONObject();JSONArray verified=new JSONArray();String error=null;File temporary=null;Class<?> effects=null,recommendations=null;ExecutorService recommendationWork=null;Object savedModel=null;String savedPreference=null;Set<String> savedCandidates=null,savedIgnored=null;
        try {
            effects=Class.forName("org.nobody.multitts.maintained.sound.SoundEffects");
            ((ExecutorService)field(effects,"IO")).submit(()->{}).get(60,TimeUnit.SECONDS);
            recommendations=Class.forName("org.nobody.multitts.maintained.sound.SceneRecommendations");
            call(recommendations,"start",new Class<?>[]{Context.class},app);
            recommendationWork=(ExecutorService)field(recommendations,"WORK");recommendationWork.submit(()->{}).get(30,TimeUnit.SECONDS);
            savedModel=field(recommendations,"model");
            if(savedModel!=null){savedCandidates=new LinkedHashSet<>((Set<String>)field(savedModel,"candidates"));savedIgnored=new LinkedHashSet<>((Set<String>)field(savedModel,"ignored"));}
            savedPreference=app.getSharedPreferences("scene-recommendations",0).getString("state",null);
            Class<?> packs=Class.forName("org.nobody.multitts.maintained.sound.SoundPacks");
            Class<?> token=Class.forName("org.nobody.multitts.maintained.VerifiedResourceDownload$Cancellation");
            File fixtures=new File(app.getExternalFilesDir("verification"),"scheduled-sounds");
            byte[] raw=Files.readAllBytes(new File(fixtures,"increment.json").toPath());
            JSONArray items=new JSONObject(new String(raw,StandardCharsets.UTF_8)).getJSONArray("items");
            require(items.length()>0&&items.length()<=24,"Bounded resource increment");
            temporary=Files.createTempDirectory(app.getCacheDir().toPath(),"scheduled-sounds-").toFile();final File owned=temporary;
            Context isolated=new ContextWrapper(app){
                @Override public File getFilesDir(){return owned;}
                @Override public Context getApplicationContext(){return this;}
                @Override public SharedPreferences getSharedPreferences(String name,int mode){return app.getSharedPreferences("qa-scheduled-sounds-"+name,mode);}
            };
            List<String> ids=new ArrayList<>();
            for(int i=0;i<items.length();i++){
                JSONObject item=items.getJSONObject(i);String id=item.getString("id");File archive=new File(fixtures,id+".zip");
                byte[] bytes=Files.readAllBytes(archive.toPath());
                require(bytes.length==item.getLong("bytes")&&sha(bytes).equals(item.getString("sha256")),"Exact candidate archive");
                call(packs,"install",new Class<?>[]{Context.class,File.class,String.class,token},isolated,archive,id,token.getConstructor().newInstance());
                require((Boolean)call(effects,"hasPack",new Class<?>[]{String.class},id),"Real production import created resource");
                Map<?,?> contexts=(Map<?,?>)call(effects,"packageContexts",new Class<?>[0]);
                require(contexts.containsKey(id),"Candidate context persisted");
                Object index=field(field(effects,"state"),"contexts");Method size=index.getClass().getDeclaredMethod("size");size.setAccessible(true);
                require((Integer)size.invoke(index)>0,"Actual audio hash and contextual rule bound");
                call(effects,"load",new Class<?>[]{Context.class},isolated);
                require((Boolean)call(effects,"hasPack",new Class<?>[]{String.class},id),"Imported resource survives reload");
                call(effects,"removePack",new Class<?>[]{Context.class,String.class},isolated,id);
                require(!(Boolean)call(effects,"hasPack",new Class<?>[]{String.class},id)&&!((Map<?,?>)call(effects,"packageContexts",new Class<?>[0])).containsKey(id),"Delete removes resource and context");
                ids.add(id);
            }
            Collections.sort(ids);for(String id:ids)verified.put(id);
            report.put("incrementSha256",sha(raw)).put("verifiedPackIds",verified).put("testedProductionApk",true).put("actualVersionCode",app.getPackageManager().getPackageInfo(app.getPackageName(),0).getLongVersionCode());
        }catch(Throwable failure){error=android.util.Log.getStackTraceString(failure);}finally{
            try{
                if(effects!=null)call(effects,"load",new Class<?>[]{Context.class},app);
                if(recommendationWork!=null){
                    recommendationWork.submit(()->{}).get(30,TimeUnit.SECONDS);SystemClock.sleep(2200);
                    final Object model=savedModel;final Set<String> candidates=savedCandidates,ignored=savedIgnored;final Class<?> rec=recommendations;
                    recommendationWork.submit(()->{try{if(model!=null){Set<String> a=(Set<String>)field(model,"candidates"),b=(Set<String>)field(model,"ignored");a.clear();a.addAll(candidates);b.clear();b.addAll(ignored);call(rec,"publish",new Class<?>[]{boolean.class},false);}}catch(Exception failure){throw new RuntimeException(failure);}}).get(30,TimeUnit.SECONDS);
                    SharedPreferences.Editor edit=app.getSharedPreferences("scene-recommendations",0).edit();if(savedPreference==null)edit.remove("state");else edit.putString("state",savedPreference);edit.commit();
                }
                if(temporary!=null)erase(temporary);
                for(String name:new String[]{"sound-pack-info","sound-pack-gain","scene-recommendations"})app.getSharedPreferences("qa-scheduled-sounds-"+name,0).edit().clear().commit();
            }catch(Throwable cleanup){error=(error==null?"":error)+android.util.Log.getStackTraceString(cleanup);}
            try{report.put("passed",error==null).put("checks",checks);if(error!=null)report.put("error",error);}catch(Exception ignored){}
            Bundle result=new Bundle();result.putString("stream",report.toString());finish(error==null?-1:0,result);
        }
    }
}
