import RealmSwift

class RealmDataManager {
    static let shared = RealmDataManager()
    
    private var realm: Realm?
    
    func configure() {
        let config = Realm.Configuration(schemaVersion: 5)
        realm = try? Realm(configuration: config)
    }
}
