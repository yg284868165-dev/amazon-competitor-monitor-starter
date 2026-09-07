#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

static NSString *const WebURL = @"http://127.0.0.1:8765";

static BOOL WebServiceIsHealthy(void) {
    NSTask *task = [[NSTask alloc] init];
    NSPipe *output = [NSPipe pipe];
    task.launchPath = @"/usr/bin/curl";
    task.arguments = @[@"--silent", @"--fail", @"--max-time", @"1",
                       [WebURL stringByAppendingString:@"/api/health"]];
    task.standardOutput = output;
    task.standardError = [NSFileHandle fileHandleWithNullDevice];

    @try {
        [task launch];
        [task waitUntilExit];
    } @catch (__unused NSException *exception) {
        return NO;
    }

    if (task.terminationStatus != 0) {
        return NO;
    }
    NSData *data = [[output fileHandleForReading] readDataToEndOfFile];
    NSString *body = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    return [body containsString:@"\"ok\":true"];
}

static void ShowError(NSString *message) {
    [NSApp activateIgnoringOtherApps:YES];
    NSAlert *alert = [[NSAlert alloc] init];
    alert.messageText = @"Amazon竞品监控";
    alert.informativeText = message;
    alert.alertStyle = NSAlertStyleCritical;
    [alert addButtonWithTitle:@"知道了"];
    [alert runModal];
}

static void OpenWebUI(void) {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL URLWithString:WebURL]];
}

@interface AppDelegate : NSObject <NSApplicationDelegate>
@property(nonatomic, strong) NSTask *serverTask;
@end

@implementation AppDelegate

- (void)configureMenu {
    NSMenu *menuBar = [[NSMenu alloc] init];
    NSMenuItem *appMenuItem = [[NSMenuItem alloc] init];
    [menuBar addItem:appMenuItem];

    NSMenu *appMenu = [[NSMenu alloc] initWithTitle:@"Amazon竞品监控"];
    NSMenuItem *openItem = [[NSMenuItem alloc] initWithTitle:@"打开管理页面"
                                                      action:@selector(openConsole:)
                                               keyEquivalent:@"o"];
    openItem.target = self;
    [appMenu addItem:openItem];
    [appMenu addItem:[NSMenuItem separatorItem]];
    NSMenuItem *quitItem = [[NSMenuItem alloc] initWithTitle:@"退出 Amazon竞品监控"
                                                      action:@selector(terminate:)
                                               keyEquivalent:@"q"];
    quitItem.target = NSApp;
    [appMenu addItem:quitItem];
    appMenuItem.submenu = appMenu;
    NSApp.mainMenu = menuBar;
}

- (BOOL)startService {
    NSString *bundlePath = [NSBundle mainBundle].bundlePath;
    NSString *projectRoot = [bundlePath stringByDeletingLastPathComponent];
    NSString *pythonPath = [projectRoot stringByAppendingPathComponent:@".venv/bin/python"];
    NSString *webEntry = [projectRoot stringByAppendingPathComponent:@"web.py"];

    BOOL isDirectory = NO;
    NSFileManager *files = [NSFileManager defaultManager];
    if (![files isExecutableFileAtPath:pythonPath] ||
        ![files fileExistsAtPath:webEntry isDirectory:&isDirectory] || isDirectory) {
        ShowError(@"无法找到程序运行环境。请将这个 App 保留在竞品监控器项目根目录中，不要单独移动 App 文件。");
        return NO;
    }

    NSString *logDirectory = [projectRoot stringByAppendingPathComponent:@"output/logs"];
    NSString *logPath = [logDirectory stringByAppendingPathComponent:@"web-ui.log"];
    NSError *directoryError = nil;
    if (![files createDirectoryAtPath:logDirectory
          withIntermediateDirectories:YES attributes:nil error:&directoryError]) {
        ShowError([NSString stringWithFormat:@"无法创建启动日志：%@", directoryError.localizedDescription]);
        return NO;
    }
    if (![files fileExistsAtPath:logPath]) {
        [files createFileAtPath:logPath contents:nil attributes:nil];
    }

    NSFileHandle *logHandle = [NSFileHandle fileHandleForWritingAtPath:logPath];
    [logHandle seekToEndOfFile];
    NSMutableDictionary *environment = [NSMutableDictionary dictionaryWithDictionary:NSProcessInfo.processInfo.environment];
    environment[@"AMAZON_MONITOR_NO_BROWSER"] = @"1";

    self.serverTask = [[NSTask alloc] init];
    self.serverTask.environment = environment;
    self.serverTask.currentDirectoryPath = projectRoot;
    self.serverTask.launchPath = pythonPath;
    self.serverTask.arguments = @[webEntry];
    self.serverTask.standardOutput = logHandle;
    self.serverTask.standardError = logHandle;

    @try {
        [self.serverTask launch];
    } @catch (NSException *exception) {
        ShowError([NSString stringWithFormat:@"管理页面启动失败：%@", exception.reason]);
        self.serverTask = nil;
        return NO;
    }

    for (NSInteger attempt = 0; attempt < 40; attempt++) {
        [NSThread sleepForTimeInterval:0.25];
        if (WebServiceIsHealthy()) {
            return YES;
        }
        if (!self.serverTask.running) {
            break;
        }
    }

    ShowError([NSString stringWithFormat:@"管理页面启动失败。请查看日志：%@", logPath]);
    self.serverTask = nil;
    return NO;
}

- (void)ensureServiceAndOpen {
    if (WebServiceIsHealthy() || [self startService]) {
        OpenWebUI();
    }
}

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [self configureMenu];
    [self ensureServiceAndOpen];
}

- (void)openConsole:(id)sender {
    [self ensureServiceAndOpen];
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    [self ensureServiceAndOpen];
    return NO;
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return NO;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    if (self.serverTask.running) {
        [self.serverTask terminate];
    }
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        application.activationPolicy = NSApplicationActivationPolicyRegular;
        AppDelegate *delegate = [[AppDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
